// TS/JS source to facts through the TypeScript compiler API (syntax only), in the shapes of telemetry/facts.py.
// Reads {root, toolchain, paths, tracked} as JSON on stdin; any failure exits non-zero with the reason on stderr.
// `typescript` loads from the toolchain folder the product's values name; exit 3 says it is not installed there.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const MARKER_RE = /^([A-Z][A-Z0-9_-]*:\s*)?telemetry:/;
const EXTS = ['.ts', '.tsx', '.js', '.mjs'];

function loadTs(toolchain) {
  const require = createRequire(path.join(toolchain, 'package.json'));
  try {
    return require('typescript');
  } catch (err) {
    process.stderr.write(`typescript is not installed under ${toolchain} (${err.message.split('\n')[0]}); ` +
      'install it there (npm install typescript)\n');
    process.exit(3);
  }
}

const input = JSON.parse(readFileSync(0, 'utf8'));
const ts = loadTs(input.toolchain);
const tracked = new Set(input.tracked);
const K = ts.SyntaxKind;

function isFunctionLike(node) {
  return ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node)
    || ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node) || ts.isGetAccessor(node)
    || ts.isSetAccessor(node) || ts.isClassDeclaration(node) || ts.isClassExpression(node);
}

function unwrap(node) {
  while (node && (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isNonNullExpression(node) || ts.isAwaitExpression(node)))
    node = node.expression;
  return node;
}

function dotted(node) {
  node = unwrap(node);
  if (!node) return null;
  if (ts.isIdentifier(node)) return node.text;
  if (node.kind === K.ThisKeyword) return 'this';
  if (ts.isPropertyAccessExpression(node)) {
    const head = dotted(node.expression);
    return head === null ? null : `${head}.${node.name.text}`;
  }
  return null;
}

function lineOf(sf, pos) {
  return sf.getLineAndCharacterOfPosition(pos).line + 1;
}

function firstArg(call) {
  const arg = call.arguments[0];
  if (!arg) return null;
  if (ts.isStringLiteral(arg) || ts.isNoSubstitutionTemplateLiteral(arg) || ts.isNumericLiteral(arg)) return arg.text;
  return dotted(arg);
}

function callRef(sf, call) {
  const callee = unwrap(call.expression);
  if (ts.isIdentifier(callee))
    return { name: callee.text, receiver: '', first_arg: firstArg(call), line: lineOf(sf, callee.getStart(sf)) };
  if (ts.isPropertyAccessExpression(callee)) {
    return {
      name: callee.name.text,
      receiver: dotted(callee.expression) ?? '?',
      first_arg: firstArg(call),
      line: lineOf(sf, callee.name.getStart(sf)),
    };
  }
  return null;
}

function refOf(sf, expr) {
  // A handler given by name or built by a call: `.catch(report)`, `.catch(make(...))`.
  expr = unwrap(expr);
  if (ts.isCallExpression(expr)) return callRef(sf, expr);
  if (ts.isIdentifier(expr)) return { name: expr.text, receiver: '', first_arg: null, line: lineOf(sf, expr.getStart(sf)) };
  if (ts.isPropertyAccessExpression(expr))
    return { name: expr.name.text, receiver: dotted(expr.expression) ?? '?', first_arg: null, line: lineOf(sf, expr.getStart(sf)) };
  return null;
}

function shallow(sf, node) {
  // Calls and throws under NODE, not descending into nested functions.
  const calls = [];
  let raises = false;
  const visit = (n) => {
    if (ts.isThrowStatement(n)) raises = true;
    if (ts.isCallExpression(n)) {
      const ref = callRef(sf, n);
      if (ref) calls.push(ref);
    }
    if (ts.isBinaryExpression(n) && n.operatorToken.kind === K.EqualsToken && ts.isPropertyAccessExpression(n.left)) {
      const target = dotted(n.left.expression);
      if (target) calls.push({ name: `${n.left.name.text}=`, receiver: target, first_arg: null, line: lineOf(sf, n.getStart(sf)) });
    }
    if (isFunctionLike(n)) return;
    ts.forEachChild(n, visit);
  };
  if (isFunctionLike(node)) {
    if (node.body) visit(node.body);
    else if (node.members) node.members.forEach(visit);
  } else visit(node);
  return { calls, raises };
}

function chainHasCatch(call) {
  let node = unwrap(call);
  while (node && ts.isCallExpression(node)) {
    const callee = unwrap(node.expression);
    if (!ts.isPropertyAccessExpression(callee)) return false;
    const name = callee.name.text;
    if (name === 'catch' || (name === 'then' && node.arguments.length >= 2)) return true;
    node = unwrap(callee.expression);
  }
  return false;
}

function resolveSpecifier(from, spec) {
  if (!spec.startsWith('./') && !spec.startsWith('../')) return null;
  const base = path.posix.normalize(path.posix.join(path.posix.dirname(from), spec));
  const stem = base.replace(/\.(js|mjs|ts|tsx)$/, '');
  const candidates = [base, ...EXTS.map((ext) => stem + ext)];
  return candidates.find((c) => tracked.has(c)) ?? null;
}

function importsOf(sf, rel) {
  const out = [];
  for (const stmt of sf.statements) {
    if (!ts.isImportDeclaration(stmt) || !ts.isStringLiteral(stmt.moduleSpecifier)) continue;
    const specifier = stmt.moduleSpecifier.text;
    const target = resolveSpecifier(rel, specifier);
    const line = lineOf(sf, stmt.getStart(sf));
    const clause = stmt.importClause;
    const base = { path: rel, target, line, specifier };
    if (!clause) {
      out.push({ ...base, local: '', name: null, type_only: false });
      continue;
    }
    const typeOnly = clause.isTypeOnly;
    if (clause.name) out.push({ ...base, local: clause.name.text, name: 'default', type_only: typeOnly });
    const bindings = clause.namedBindings;
    if (bindings && ts.isNamespaceImport(bindings))
      out.push({ ...base, local: bindings.name.text, name: null, type_only: typeOnly });
    if (bindings && ts.isNamedImports(bindings)) {
      for (const el of bindings.elements) {
        out.push({
          ...base,
          local: el.name.text,
          name: (el.propertyName ?? el.name).text,
          type_only: typeOnly || el.isTypeOnly,
        });
      }
    }
  }
  return out;
}

function markersOf(sf, rel, text) {
  if (!text.includes('telemetry:')) return [];
  const seen = new Map();
  const take = (ranges) => {
    for (const r of ranges ?? []) {
      if (r.kind !== K.SingleLineCommentTrivia || seen.has(r.pos)) continue;
      const body = text.slice(r.pos + 2, r.end).trim();
      if (MARKER_RE.test(body)) seen.set(r.pos, { path: rel, line: lineOf(sf, r.pos), text: body });
    }
  };
  const visit = (node) => {
    if (node.kind < K.FirstNode || node.kind === K.EndOfFileToken) {
      take(ts.getLeadingCommentRanges(text, node.pos));
      take(ts.getTrailingCommentRanges(text, node.end));
      return;
    }
    node.getChildren(sf).forEach(visit);
  };
  visit(sf);
  return [...seen.values()].sort((a, b) => a.line - b.line);
}

function functionName(node) {
  if ((ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node)) && node.name && ts.isIdentifier(node.name))
    return { name: node.name.text, method: ts.isMethodDeclaration(node) && ts.isClassLike(node.parent) };
  const parent = node.parent;
  if ((ts.isArrowFunction(node) || ts.isFunctionExpression(node)) && parent) {
    if (ts.isVariableDeclaration(parent) && ts.isIdentifier(parent.name)) return { name: parent.name.text, method: false };
    if (ts.isPropertyDeclaration(parent) && ts.isIdentifier(parent.name)) return { name: parent.name.text, method: true };
  }
  return null;
}

function scanFile(rel) {
  const text = readFileSync(path.join(input.root, rel), 'utf8');
  const kind = rel.endsWith('.tsx') ? ts.ScriptKind.TSX : rel.endsWith('.ts') ? ts.ScriptKind.TS : ts.ScriptKind.JS;
  const sf = ts.createSourceFile(rel, text, ts.ScriptTarget.Latest, true, kind);
  if (sf.parseDiagnostics?.length) {
    const d = sf.parseDiagnostics[0];
    throw new Error(`${rel}:${lineOf(sf, d.start)}: ${ts.flattenDiagnosticMessageText(d.messageText, '\n')}`);
  }
  const sites = [];
  const functions = [];
  const allCalls = new Map();
  const scopes = [];
  const scopeText = () => (scopes.length ? scopes[scopes.length - 1].getText(sf) : text);

  const handlerSite = (kindName, anchor, handler) => {
    const line = lineOf(sf, anchor.getStart(sf));
    const fn = unwrap(handler);
    if (ts.isArrowFunction(fn) || ts.isFunctionExpression(fn)) {
      const { calls, raises } = shallow(sf, fn);
      const bodyLine = lineOf(sf, fn.body.getStart(sf)) + (ts.isBlock(fn.body) ? 1 : 0);
      return { kind: kindName, line, detail: kindName, body_line: bodyLine, calls, raises, handler: null };
    }
    return { kind: kindName, line, detail: `${kindName}(${handler.getText(sf).slice(0, 60)})`, body_line: line + 1, calls: [], raises: false, handler: refOf(sf, handler) };
  };

  const visit = (node) => {
    if (ts.isCatchClause(node)) {
      const { calls, raises } = shallow(sf, node.block);
      const detail = node.variableDeclaration ? `catch (${node.variableDeclaration.getText(sf)})` : 'catch';
      sites.push({ kind: 'catch', line: lineOf(sf, node.getStart(sf)), detail, body_line: lineOf(sf, node.block.getStart(sf)) + 1, calls, raises, handler: null, scope_text: scopeText() });
    }
    if (ts.isCallExpression(node)) {
      const ref = callRef(sf, node);
      if (ref) allCalls.set(`${ref.receiver}|${ref.name}|${ref.first_arg}`, ref);
      const callee = unwrap(node.expression);
      if (ts.isPropertyAccessExpression(callee)) {
        const name = callee.name.text;
        if (name === 'catch' && node.arguments.length === 1)
          sites.push({ ...handlerSite('.catch', callee.name, node.arguments[0]), scope_text: scopeText() });
        if (name === 'then' && node.arguments.length === 2)
          sites.push({ ...handlerSite('.then', callee.name, node.arguments[1]), scope_text: scopeText() });
      }
    }
    if (ts.isVoidExpression(node) && ts.isCallExpression(unwrap(node.expression)) && !chainHasCatch(node.expression)) {
      const line = lineOf(sf, node.getStart(sf));
      sites.push({ kind: 'floating', line, detail: `void ${unwrap(node.expression).expression.getText(sf).slice(0, 60)}`, body_line: line + 1, calls: [], raises: false, handler: null, scope_text: scopeText() });
    }
    const named = functionName(node);
    if (named) functions.push({ path: rel, ...named, calls: shallow(sf, node).calls });
    const isScope = isFunctionLike(node) && !ts.isClassLike(node);
    if (isScope) scopes.push(node);
    ts.forEachChild(node, visit);
    if (isScope) scopes.pop();
  };
  visit(sf);
  return {
    path: rel,
    sites: sites.map((s) => ({ path: rel, ...s })),
    functions,
    imports: importsOf(sf, rel),
    markers: markersOf(sf, rel, text),
    calls: [...allCalls.values()],
  };
}

const files = {};
for (const rel of input.paths) {
  try {
    files[rel] = scanFile(rel);
  } catch (err) {
    process.stderr.write(`ts_scan: ${err.message}\n`);
    process.exit(2);
  }
}
process.stdout.write(JSON.stringify({ files }));

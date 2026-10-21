Run each factory command plainly: on one line that starts with the word factory, with no pipe, redirect, `&`,
heredoc or subshell, and no `$` or backtick outside single quotes. Read its answer as printed. A session may put other
commands before or after it, joined by `;`, `&&` or a new line; a subagent runs it alone, as the whole call, or after
one `cd <folder> &&`. A body or a long text goes in a file in your worktree's .factory folder, which git ignores, named
as an argument. A tester writes only in its own folder there, `.factory/<its agent id>/`: the `folder` that `factory
ready-check` or `factory qa scenario` answers with. If you write no files, keep the text short and in single quotes. A
spec your brief names by its path, open with Read.

Run each factory command plainly, as a command of its own: on one line that starts with the word factory, with no
pipe, redirect, `&`, heredoc or subshell, and with any `$`, backtick or wildcard (`*`, `?`, `[`, `{`) inside single
quotes. Read its answer as printed. Other commands may come before or after it, joined by `;`, `&&`, `||` or a new
line: `date +%s; factory slot sign-in …` runs, while `factory … | head` and `L=$(factory …)` are refused. A body or a
long text goes in a file in your worktree's .factory folder, which git ignores, named as an argument. A tester writes
only in its own folder there, `.factory/<its agent id>/`: the `folder` that `factory ready-check` or `factory qa
scenario` answers with. If you write no files, keep the text short and in single quotes. A spec your brief names by its
path, open with Read.

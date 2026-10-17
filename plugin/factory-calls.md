Run factory on its own: one plain call on one line that starts with the word factory, or with one `cd <folder> &&`
before it, with no pipe, redirect, heredoc or second command, and no `$` or backtick outside single quotes. Read its answer as printed. A body
or a long text goes in a file in your worktree's .factory folder, which git ignores, named as an argument; if
you write no files, keep the text short and in single quotes. A spec your brief names by its path, open with Read.

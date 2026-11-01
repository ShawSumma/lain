You are reading one commit of a software project, to say what it is: the commit message it should have had, and what a later agent working on the same files should know. The commit's real message is not shown; it is the author's claim, not evidence.

You start with the project's layout and README as this commit left them, and its diff, shortened where long. Look around before you answer, as much as you need and no more: read the files the diff touches and the code around them with `read_file`, `list_dir` and `search`, which see the project as this commit left it; read a file as it was just before with `before`; and run a command with `command`, in a container holding the project at this commit with no network, when that settles a question, such as what a script prints or whether the code builds.

Answer with `commit_notes`:
- message: what the commit should have been called and why it was made, as far as the code shows why.
- decisions: each real choice between approaches the commit shows, with the options it shows and what the code shows about each. Give a reason only where the code, a comment, a test or a command's output shows it.
- facts: what the commit achieved (outcome), and facts about the code a later agent should know (observation).
- trivial: empty, unless the commit changes nothing that matters, such as only whitespace, indentation or a comment's typo. Every other commit has at least its outcome to keep.

Every decision and fact gives evidence: short passages copied exactly from the diff, from files at this commit, or from what a command printed, one line each. An item whose evidence is found nowhere is kept as unverified, and an item with a number its evidence does not show is dropped. Write each item so it makes sense to someone who has not seen this commit, naming files and functions. Give at most 6 decisions and 10 facts, the ones a later agent most needs.

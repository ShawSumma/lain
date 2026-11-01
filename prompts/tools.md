## before
Read a whole file as it was just before this commit, in its parent.

## before.path
The file, relative to the project root, by the name it had in the parent.

## command
Run a shell command in a container holding the project as this commit left it, with no network, such as `make -n`, `python3 -c 'import x'` or `grep -rn name src`. Returns its exit status and output, cut when long.

## command.command
The command, run with sh -c in the project's root.

## commit_notes
Give what this commit is, once you understand it.

## commit_notes.message
The commit message it should have had: a subject line under 72 characters in the imperative, then a blank line and a body saying what changed and why, as far as the code shows why.

## commit_notes.trivial
Empty, unless the commit changes nothing that matters, such as only whitespace, indentation or a typo in a comment; then say so in one line, and the lists may be empty.

## commit_notes.decisions
Each real choice between approaches the commit shows: one implementation replaced by another, an approach taken out, a design picked.

## commit_notes.decisions.chose
What was chosen, naming the files and functions, so it makes sense to someone who has not seen the commit.

## commit_notes.decisions.options
The options the commit shows, the chosen one and those replaced or not taken, each with what the code shows about it.

## commit_notes.decisions.options.option
The option.

## commit_notes.decisions.options.verdict
What the code shows about it: why it was taken or dropped, only as far as the code, a comment or a test shows.

## commit_notes.decisions.options.taken
Whether this is the option the commit took.

## commit_notes.decisions.files
The files it is about, relative to the project root.

## commit_notes.decisions.evidence
Short passages copied exactly from the diff, the files at this commit or what a command printed, one line each, enough to show it.

## commit_notes.facts
What a later agent working on these files should know: what the commit achieved (outcome), and facts about the code (observation), such as a function that must be called before another.

## commit_notes.facts.kind
outcome or observation.

## commit_notes.facts.text
The fact, naming the files and functions.

## commit_notes.facts.files
The files it is about, relative to the project root.

## commit_notes.facts.evidence
Short passages copied exactly from the diff, the files at this commit or what a command printed, one line each, enough to show it.

## list_dir
List one directory, its directories first, each marked (directory), then its files, each with how many lines it has.

## list_dir.path
The directory, relative to the project root; . for the root.

## read_file
Read a whole file.

## read_file.path
The file, relative to the project root.

## search
Find the lines matching a regular expression. Returns file:line: text.

## search.pattern
A Python regular expression, searched for anywhere in each line, such as `mx_jit_\w+` or `L_ADD_I64|L_SUB_I64`.

## search.paths
The files and directories to look in, as a list of strings, such as ["src", "README.md"]; ["."] for everything.

## find
Find the places described in plain words, such as "every switch without a default" or "where a version's code stream is built". Each listed file, or each file under a listed directory, is read separately and in parallel. Returns file:line: text (why).

## find.question
What to look for, in plain words.

## find.paths
The files and directories to read, as a list of strings, such as ["src/jit"]; ["."] for everything.

## send
Send a letter to another agent, a Claude Code session or a program, by its number such as #3 or its name such as lain.harness.plan; a name ending in .* reaches everyone under it. Their answer comes as a letter in a later note, not as this call's result.

## send.to
The number, such as #3, the name, or a name ending in .* for everyone under it.

## send.text
The letter, saying what you need and why, so it makes sense to someone who has not seen your work.

## who
List everyone who can be sent a letter, by number and name.

## list_addresses
List everyone who can be sent a letter now, by number and name: the Claude Code sessions still running, lain's own agents and the programs that took an address.

## send_letter
Send a letter to another agent, a Claude Code session or a program, by its number such as #3 or its name such as lain.harness.plan; a name ending in .* reaches everyone under it. An answer comes back as a letter to the sender's address.

## send_letter.sender
Your own address: for a Claude Code session, the one it was given when it started, such as lain.session.1a2b3c4d; otherwise a dotted lowercase name of your own, such as lain.ci.

## read_letters
Take the letters for an address that have not been read yet; each is given once. A Claude Code session gets its letters as it works without asking.

## read_letters.address
The address to read for, such as lain.session.1a2b3c4d.

## start
Queue some of lain's jobs for this project. They run inside lain's process one at a time, in the order queued, and go on after the session ends; a job already queued or running is not queued twice. git reads the git history in and has a model extract each commit not read before, one call per commit; reviews reads closed pull requests through gh and extracts them; sessions reads this project's Claude Code sessions in and extracts their finished stretches; recheck has a light model check the memories that may be out of date; map has a light model write lain's map of the project, a page per region, again only where the code changed. Start jobs only when the user asks.

## start.jobs
The jobs, in the order to run them, from git, reviews, sessions, recheck and map.

## stop
Stop some of lain's jobs: a running one ends once the model calls it has in flight come back, and a queued one is taken off the queue. What a job remembered so far is kept, and starting it again carries on from there.

## list_jobs
Say, for each of lain's jobs in this project, whether it is running, waiting in the queue, or how it last ended.

## show_job
Say how one of lain's jobs is going or how it last ended, with the end of its output.

## show_job.job
The job: git, reviews, sessions, recheck or map.

## show_page
The address of lain's page for this project: every job, session and model call as a tree of what each did, live as it happens. Give it to the user.

## report_bug
Record a bug in lain itself, not in this project, so it can be fixed in lain later: a wrong, missing or misleading answer, or something lain did that it should not have. An error lain gives is recorded already, with its traceback, and names its bug; report one only when the user asks, or to add what that error leaves out.

## report_bug.text
What happened, so that someone fixing lain who has not seen this session can follow it: what lain was asked, what it did or said, what was expected, and the bug's id if lain's error named one.

## list_bugs
List the bugs lain has recorded, newest first: errors it caught, records it could not read, and bugs reported.

## show_bug
Show one bug lain has recorded in full, with its traceback or report.

## show_bug.id
The bug's id, from list_bugs or from the error that named it.

## changes
Show everything you have changed so far, as a diff against the files you started from.

## commit
Show one commit's date and diff.

## commit.number
The commit's position on the main line, 1 for the first.

## touching
List every commit that changed a path: its position, its date, the subject already written for it, and each file it changed.

## touching.path
The file or directory, relative to the project root.

## write_file
Create or replace a whole file. Replacing returns the change as a diff.

## write_file.path
The file, one of those this task may change.

## write_file.content
The whole new content of the file.

## edit_file
Replace the one exact occurrence of old with new. Returns the change as a diff.

## edit_file.path
The file, one of those this task may change.

## edit_file.old
The exact text to replace; it must occur exactly once.

## edit_file.new
The text to put in its place.

## delete_file
Delete one file.

## delete_file.path
The file, one of those this task may change.

## run
Run one of the project's commands in your working copy. Returns its exit status, the files in the working copy it read and changed, and its output.

## run.command
Which command to run.

## report
Report the change and finish.

## report.summary
What you changed, in one or two sentences.

## report.verified
Claims a tool call showed, each naming that call.

## report.inferred
Claims you believe but did not observe, each with the reason.

## report.rejected
Approaches you considered and did not take, each with why.

## report.concerns
Anything a reviewer or a later task should know.

## give_up
Stop without finishing the task, and say why. Use it when the task cannot be done as given; a later plan starts from your reason.

## give_up.reason
What stops the task, and what you found that shows it.

## ask
Ask the planner something only it or another task knows, and wait for the answer.

## ask.question
The question, with what you need the answer for.

## answer
Answer a worker's question, and keep the plan as it is.

## answer.answer
The answer, from what you planned and what you can read.

## plan
Give the plan and finish.

## plan.goal
What is to be achieved, in the project's own terms.

## plan.options
Each approach you considered.

## plan.options.option
The approach.

## plan.options.verdict
Why you would take it, or why not.

## plan.options.taken
Whether this is the approach you are taking.

## plan.chose
The approach you chose, and what makes it right here.

## plan.replaces
The ids of the decisions in the leads that this plan overturns, such as a3f09c21b7e4. A decision is retired only for the files the accepted work changes. Empty when the plan overturns none.

## plan.assumptions
What must stay true for the chosen approach to stay right, such as "one process writes the file"; empty when nothing does.

## plan.revisit_when
What would mean the approach should be chosen again, such as "a second writer is added"; empty when nothing would. Write a condition lain can check as one of `file: PATH` (it changes), `date: YYYY-MM-DD` (it passes), `dependency: NAME` (its declared version changes) or `dependency: NAME >= VERSION`; anything else stays prose.

## plan.commands
The commands workers and reviewers may run in their working copies, such as building and the tests. Each is run exactly as given.

## plan.commands.name
A short name for the command, such as build or parity.

## plan.commands.argv
The program and its arguments, such as ["make", "-j8"].

## plan.tasks
The tasks, in the order they must happen.

## plan.tasks.name
A short name for the task in lowercase letters, digits and dashes, such as jit-encoder. The worker doing it is known by this name.

## plan.tasks.task
The change to make, in full. The worker sees only this and the files.

## plan.tasks.files
Every file this task may create or change.

## plan.tasks.after
The names of earlier tasks that must be accepted before this one starts. Tasks with nothing left to wait for and no files in common run at the same time.

## plan.tasks.done_when
What must be true when the task is finished, stated so that a reviewer can tell whether it holds.

## plan.rules
Each standing rule the request itself gives for work beyond this request, never one from the leads; empty when it gives none.

## plan.rules.rule
The rule, in one or two sentences that make sense to someone who has not read the request.

## plan.rules.quote
The user's words that give the rule, copied exactly from the request.

## plan.rules.files
The files the rule covers, relative to the project root; a directory ending in /, a glob, or . for the whole project.

## worker_finish
Report the change, or give up on the task, and finish.

## worker_finish.finish
report if the change is made, give_up if the task cannot be done as given, ask to put a question to the planner.

## worker_finish.summary
What you changed, in one or two sentences; empty when giving up.

## worker_finish.verified
Claims a tool call showed, each naming that call.

## worker_finish.inferred
Claims you believe but did not observe, each with the reason.

## worker_finish.rejected
Approaches you considered and did not take, each with why.

## worker_finish.concerns
Anything a reviewer or a later task should know.

## worker_finish.reason
When giving up, what stops the task and what you found that shows it; empty otherwise.

## worker_finish.question
When asking, the question for the planner, with what you need the answer for; empty otherwise.

## extract
Give what this stretch of work decided and established, and finish.

## extract.decisions
Each choice between approaches that shaped the code, including approaches tried and abandoned.

## extract.decisions.chose
The approach taken, and what made it right here.

## extract.decisions.options
Each approach that was considered or tried, including the one taken.

## extract.decisions.options.option
The approach.

## extract.decisions.options.verdict
Why it was taken, or why it was dropped.

## extract.decisions.options.taken
Whether this is the approach that was kept.

## extract.decisions.files
The files the decision is about, relative to the project root. For a convention that holds more widely, a directory ending in /, such as src/, a glob, such as src/*.py, or . for the whole project.

## extract.decisions.cites
The numbers of the steps that show the decision being made, or where the agent or the user said why.

## extract.decisions.quotes
Short passages copied exactly from the cited steps that show the decision and why: from a tool's output or the call it answers, the user's words, or what the agent said.

## extract.decisions.assumptions
What the record shows must stay true for the decision to stay right; empty when it shows none.

## extract.decisions.revisit_when
What the record shows would mean the decision should be made again; empty when it shows none. Write a condition lain can check as one of `file: PATH` (it changes), `date: YYYY-MM-DD` (it passes), `dependency: NAME` (its declared version changes) or `dependency: NAME >= VERSION`; anything else stays prose.

## extract.decisions.replaces
The ids of the earlier decisions, listed after the record, that this decision overturns. Empty when it overturns none; working on the same file is not overturning.

## extract.claims
Each thing the work established or found out.

## extract.claims.kind
outcome for what the work achieved, observation for a fact learned about the code, the tools or the machine, or a rule the user gave.

## extract.claims.text
The claim, in one or two sentences that make sense without the transcript.

## extract.claims.files
The files the claim is about, relative to the project root. For a rule or fact that holds more widely, a directory ending in /, such as src/, a glob, such as src/*.py, or . for the whole project.

## extract.claims.cites
The numbers of the steps that show the claim is true: the tool results that show it, or, when only someone's words show it, the step where it was said.

## extract.claims.quotes
Short passages copied exactly from the cited steps that show the claim: for what the work did or found, from a tool's output or the call it answers; for a rule, from the user's words.

## extract.claims.restates
For a rule the user gave, the ids of the earlier rules, listed after the record, that it says again in other words. Empty for anything else, and for a rule that is new.

## extract.continues
The ids of the earlier goals, listed after the record, that this stretch carries on with, such as a feature it keeps building; empty when it starts something new.

## show_memory
What lain remembers about one file of the project, as Markdown: the user's rules, the decisions that hold and what they replaced, what was turned down and learned, and which sessions changed and read it.

## show_memory.path
The file, relative to the project root.

## recall
Find what lain has that best matches some words: stories, memories and files, then the regions and files of lain's map; and say which words nothing recorded mentions.

## recall.query
The words to look for, such as what the code does, a class name or what was decided.

## list_stories
List the stretches of earlier work, newest first, each with its ref, what was asked, how many files it changed and how much it left in memory.

## show_story
One stretch of earlier work as Markdown: the request word for word, the rules given, the decisions with what was turned down, and what was done and learned.

## show_story.ref
The story, as SESSION/STEP from list_stories or the step a memory says it came from, such as lain#64dd6764/137.

## list_decisions
The decisions lain holds for the project, or for one file, with what each assumes and when to revisit it.

## list_decisions.path
The file, relative to the project root; empty for every decision.

## contradicted
Give the items the record contradicts, and finish.

## contradicted.contradicted
Each item something in the record contradicts; empty when none is.

## contradicted.contradicted.item
The item's number.

## contradicted.contradicted.why
What contradicts it, naming the step.

## remember
Record something about the project for later agents: a fact about the code, or a decision with why it was made. It is kept as stated by an agent, below what a tool showed.

## remember.text
The fact or decision, in one or two sentences that make sense to someone who was not there, naming files and functions.

## remember.decision
true for a choice between approaches, false for a fact.

## remember.files
The files it is about, relative to the project root; a directory ending in /, a glob, or . for wider scopes.

## recheck
Say whether the memory still holds, and finish.

## recheck.verdict
holds if the code still bears it out, outdated if it no longer does, changed if part of it still holds.

## recheck.text
For changed, required: the memory as it should read now, naming files and functions as they are now. Empty for holds and outdated.

## recheck.files
For changed, the files the memory is about now, relative to the project root, or a directory ending in /, a glob, or . for wider scopes; empty to keep the files it has. Empty for holds and outdated.

## recheck.reason
What in the code now shows the verdict, naming files and functions.

## describe
Give the region's page of the map, and finish.

## describe.summary
One line saying what the region is for, in the project's own terms.

## describe.text
At most about 200 words: what the region does and how, where to start, how it fits with what it uses and what uses it, and the conventions the code shows.

## show_map
lain's map of one file or directory of the project, as Markdown: what it is for, its parts, what it declares, the code it uses and that uses it, what changes with it, and the rules and decisions that hold there.

## show_map.path
The file or directory, relative to the project root; . or empty for the whole project.

## archaeology
Give the new commit message and what this commit decided and established, and finish.

## archaeology.subject
One line in the imperative saying what the commit does for the project, naming the idea rather than the files.

## archaeology.body
Short paragraphs on what the change addresses, how it works at the level of the design, and why, naming what shows it; empty when the subject says everything.

## findings
Give the review and finish. An empty list accepts the change.

## findings.findings
Each thing that is wrong.

## findings.findings.where
The file, and the line or the name of the thing.

## findings.findings.wrong
What is wrong, and what goes wrong because of it.

## findings.findings.fix
The change you would make.

## matches
Give every place in the file that answers the question, and finish.

## matches.matches
Each place that answers the question.

## matches.matches.line
The line number.

## matches.matches.text
The text of that line.

## matches.matches.why
Why it matches, in a few words.

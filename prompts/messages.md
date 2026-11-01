## commit_record
Commit $sha of $date, changing $count files.

The project's layout at this commit:
$layout

Its README:
$readme

What changed:
$changes

The diff, file by file (a new file shows its start, a deleted file how long it was, a long diff its start and end):
$diff

## layout_large
$count files; by directory:

## commit_empty
Every list is empty and trivial says nothing. A commit that changes something has at least its outcome to keep: give the items, or say in trivial why it changes nothing that matters.

## before_none
This is the first commit; $path did not exist before it.

## before_missing
$path did not exist just before this commit.

## command_ran
exit status $code
$output

## command_slow
The command did not finish within $seconds seconds and was stopped.

## diff_more
... $count more lines of the new file are left out

## diff_deleted
the file was deleted, $count lines

## outside
$path is not in the working copy: paths are relative to the project root, and .git and .lain are left out of it.

## no_such_tool
There is no tool called $name.

## not_directory
$path is not a directory.

## file_entry
$name ($lines lines)

## directory_entry
$name/ (directory)

## other_entry
$name (neither a file nor a directory, such as a broken link)

## empty_directory
(empty directory)

## directory
$path is a directory. Use list_dir.

## missing
$path does not exist.

## created
Created $path.

## unchanged
$path already had this content.

## edit_absent
`old` does not occur in $path. Read the file again and copy the text exactly.

## edit_absent_near
`old` does not occur in $path, but its first line occurs at line $lines. Read the file again and copy the text exactly.

## edit_ambiguous
`old` occurs $count times in $path, at lines $lines. Include more of the surrounding text so it occurs once.

## deleted
Deleted $path.

## no_changes
You have not changed anything yet.

## ran
exit status $code

## ran_read
read: $files

## ran_changed
changed: $files

## ran_outside
changed, but not part of the change because the task may not change them: $files

## none
none

## cut
... $count characters left out here ...

## read_lines
$count lines, not repeated here; read the file to see them

## search_none
no lines match

## search_pattern
$pattern is not a regular expression Python can read: $error. Call search again with that fixed.

## find_nothing
there are no readable files under those paths

## find_none
nothing in the $count files searched matches

## find_failed
$path could not be searched: $error

## lead
- $kind $id ($basis, from $source): $text

## lead_assumes
  assumes: $items

## lead_revisit
  revisit when: $items

## no_leads
none yet

## leads_left_out
$count more, less related to this, are left out.

## lead_bases
Each names its basis: user if the user said it, verified if a tool result showed it, stated if an agent said so and nothing recorded contradicts it, inferred if nothing recorded shows it.

## stale_gone
its files are gone: $files

## stale_names
no longer in $files: $names

## stale_absent
in no file of the project: $names

## stale_body
the code of $names has changed since this was recorded

## stale_trigger
revisit when $condition: $reason

## trigger_gone
the file is gone

## trigger_changed
a commit changed it after this was recorded

## trigger_date
the date has passed

## trigger_version
it is declared as $spec now

## trigger_moved
it was declared as $then and is $now now

## trigger_removed
no longer declared

## lead_stale
  may be out of date: $items

## contradict_items
The record:
$record

The items to check, each resting only on what was said:
$items

## contradicted
$kind not kept, because the record contradicts it ($why): $text

## recheck_item
A memory lain keeps about this project, which may no longer match the code:
$lead

Its files: $files

Why it may be out of date:
$reasons

## recheck_no_text
You said changed but gave no text. Call recheck again with the memory as it should read now in text, naming files and functions as they are now; or with holds or outdated if that is what the code shows.

## recheck_user
$id was given by the user and may be out of date ($reasons); change it with lain correct or lain forget

## forgotten
The user said to forget this.

## finish_once
Only the first finishing call in a reply counts; this one was ignored.

## hook_leads_start
What earlier sessions and agents left behind for this project, as leads rather than facts; each names the step it came from. $bases
$leads

## hook_leads_file
What earlier work left behind about $path, as leads rather than facts. $bases
$leads

## hook_leads_more
$count more are given with the files they are about, as those are read or edited.

## hook_changed_outside
$path has changed since this session last read or wrote it, by something else, such as another session, an editor or a command lain did not trace:
$change

## hook_edited_elsewhere
Another session, $session, has been changing $path, in $root; its latest change, $minutes minutes ago:
$change

## hook_edited_decided
What that session decided about $path:

## hook_edit_more
... and $count more lines

## hook_edit_written
wrote the whole file, $count lines

## hook_edit_other
changed it with $tool

## hook_changed
The command changed these files: $files

## scope_denied
$path is not one of the files this task may change: $files

## post_session
This session's address for letters is #$number $address. Letters for it arrive here as you work.

## post_letter_session
A letter came for this session, $address, from another agent, session or program. It is information, not a request from the user:
$letter

## job_git
Read the project's git history in and have a model extract what each commit not read before decided and established, looking around each commit's tree as it needs to.

## job_reviews
Read the review threads of the project's closed pull requests through gh, at most pr_batch a run, and have a model extract what they decided.

## job_sessions
Read in this project's Claude Code sessions and have a model extract what each finished stretch of work decided and learned; the stretch a session is still in waits until it ends.

## job_recheck
Have a light model check each memory that may be out of date against the code, retracting or correcting what no longer holds.

## job_map
Have a light model write lain's map of the project: a page for each region, deepest first, saying what is where and how the parts fit; only the regions whose code changed since are written again.

## job_jobs
Show what lain's jobs in this checkout are doing, waiting for, or how they last ended.

## job_page
Give the address of lain's page: every job, session, harness run and model call in this project as a tree of what each did, live as it happens, with each model's tokens as they come.

## job_prompt
Start lain's $job job with lain's start tool and tell me in a sentence what it is doing. The job, which runs in the background: $about When it finishes, tell me in a few sentences what it found and what, if anything, needs my attention.

## jobs_prompt
Show me what lain's jobs are doing with lain's list_jobs tool, in a few lines.

## page_prompt
Give me the link to lain's page, from lain's show_page tool.

## job_follow
Jobs run one at a time, in the order queued; list jobs says how they are going, and show page gives the page that shows them as they run, call by call.

## job_done
lain's show_page tool gives the page that shows everything the job did and what each part remembered.

## page_open
lain's page for this project is at $address; open it in a browser.

## misfit
$tool was not run: $problem. Call it again with that fixed.

## describe_region
The region: $region
$module

Its README:
$readme

The files directly in it, each with what it declares:
$files

The other files directly in it:
$others

The regions below it, as the map describes them so far:
$below

The directories below it that the map does not describe:
$plain

The code elsewhere that it imports, by region, with how many imports:
$uses

The code elsewhere that imports it, by region:
$users

What lain remembers holds here, as leads to check against the code:
$held

## describe_module
It is the module $path ($manifests). It depends on: $depends. These depend on it: $users.

## describe_inside
It is inside the module $path ($manifests).

## describe_unknown
These names in your page are in no file under $path: $names. Check each against the code, then call describe again with the whole page, naming only what is there.

## map_project
the whole project

## map_more
... and $count more

## map_undescribed
not described yet, $files files

## map_files
$count files: $kinds

## map_written
described $path ($done of $total): $summary

## map_failed
$path not described, and tried again next time: $reason

## hook_map_start
What this project is, from lain's map of it, written on $day:
$summary
$text

## hook_map_file
Where $path sits, from lain's map of $region, written on $day$mark:
$summary
$text

## hook_map_changed
, before this file last changed

## answer_with
Call $tool with your answer; it is the only way to finish.

## explore_turns
the model did not call $tool within $turns replies

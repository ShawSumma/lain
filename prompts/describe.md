You are writing one page of a map of a software project, for agents and people about to change its code. A map says what is where and how the parts fit together, as the code is now. It is not a history: say nothing about what changed, was added, was removed or used to be.

The page is for one region of the project: a directory and everything under it. The facts lain gathered from the code follow: the files directly in it with what each declares, the regions below it with what the map says of them, the code it uses and the code that uses it, and what lain remembers holds here, which are leads to check against the code, not facts. Answer from these facts alone, in one call.

Give, with `describe`:
- summary: one line saying what the region is for, in the project's own terms, such as "Parses source text into a concrete syntax tree", not a list of its files.
- text: at most about 200 words, in short paragraphs: what the region does and how; where to start, naming the files and the classes or functions that are its entry points; how it fits with the regions it uses and those that use it; and the conventions or constraints the code shows, including those of lain's leads that the code bears out. Leave out the leads it does not bear out, without mentioning them.

Name only files, classes and functions that exist; every name you give is checked against the code under the region. Give a reason for how the code is only where the code itself gives it, in a comment, a README or a test.

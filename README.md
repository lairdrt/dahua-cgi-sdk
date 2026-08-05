# "What is this project?"

Colaborative effort between ChatGPT and the human author.

"The SDK models the recorder, not the protocol." - cgpt

## Workflow

Our implementation protocol

I'll use these headings consistently:

---
Discussion only

No repository changes.
---
Review only

Code or API for discussion.

Don't paste anything yet.
---
IMPLEMENT

I'll provide:

- the exact filename
- the complete contents of the file
- any new imports
- any new dependencies (if applicable)

You replace/create exactly that file.
---
BUILD & TEST

I'll tell you exactly what commands to run.

We'll fix anything that fails before moving on.
---
COMMIT

I'll recommend a commit message.

No commits until we've both agreed the feature is complete.
---
PUSH

Push the feature branch.
---
PULL REQUEST

We'll review what changed before merging.
---

## Implementation Steps

Implement

↓

black .

↓

ruff check .

↓

pytest

↓

commit
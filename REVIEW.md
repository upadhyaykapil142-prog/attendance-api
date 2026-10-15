# REVIEW.md

## Defects found in the starter code

The current implementation has been updated during development. The following table should document only defects confirmed in the original starter `app/main.py`, not issues guessed from the final implementation.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Original starter `app/main.py` — to be verified | Original defect not yet documented | Compare the original implementation with the relevant assignment requirement and reproduce the failure | Record the actual correction made in the implementation |

## Items considered but not automatically defects

- A missing attendance record is not necessarily a defect; employees may have no attendance log for a given day.
- A duplicate punch-in must be rejected safely by a database uniqueness constraint, not just an application-level pre-check.
- Analytics should use stored derived attendance fields where the assignment requires it.
- Indexes should be justified by query patterns and verified using execution plans.
- These are review criteria, not claims that the original starter code contained these defects.

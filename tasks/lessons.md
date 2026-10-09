# Lessons
- Control chars (\n, \t) must become spaces *before* stripping control chars, or words glue together.
- Do not hold an in-flight "dialing" key past the TCP connect phase: reconnect raced and was silently ignored.
- sv-ttk needs the root passed on a second Tk root; theme is per-root.
- Test helpers that wait for "an event exists" must count events before the action (reconnect case).

---
description: Save structured memory to north9 before compacting
---

Before compacting, save work to north9 memory so nothing is lost.

1. For each completed step: memory_mark_completed("action → exact result")
2. For each dead end: memory_mark_failed("what failed → exact error")
3. For next steps: memory_add_pending("concrete action")
4. For exact values: memory_anchor("key: value")
5. memory_save()
6. memory_get() — show me the saved state

Then compact normally. Memory reloads automatically next session.

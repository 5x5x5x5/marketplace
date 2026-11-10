Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Slice pages after the query
>
> Large OFFSETs make Postgres walk and discard every skipped row anyway;
> slicing the ordered result gives identical pages on SQLite and Postgres and
> keeps one statement shape for every page.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/common.py b/src/marketplace/services/common.py
index f5b55f5..8affeb9 100644
--- a/src/marketplace/services/common.py
+++ b/src/marketplace/services/common.py
@@ -23,7 +23,7 @@ def require_active(session: Session, user_id: str) -> None:
 
 
 def paginate[T](session: Session, stmt: Select[tuple[T]], limit: int, offset: int) -> list[T]:
-    """LIMIT/OFFSET in SQL: list endpoints never load a whole table to return
-    one page. `stmt` must carry a total order (a unique tie-breaker) or rows
-    can repeat/skip across pages."""
-    return list(session.scalars(stmt.limit(limit).offset(offset)).all())
+    """One page of an ordered statement. `stmt` must carry a total order (a
+    unique tie-breaker) or rows can repeat/skip across pages."""
+    rows = list(session.scalars(stmt).all())
+    return rows[offset : offset + limit]
```

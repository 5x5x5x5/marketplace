Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Commit in the session dependency
>
> Committing inside a route-class wrapper meant every router had to opt in,
> and a session used outside a CommitRoute never committed. Commit where the
> session is created, after the handler returns, and keep CommitRoute as the
> router marker the startup check looks for.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/db.py b/src/marketplace/db.py
index c6658f5..504ac19 100644
--- a/src/marketplace/db.py
+++ b/src/marketplace/db.py
@@ -19,7 +19,6 @@ from fastapi.routing import APIRoute
 from sqlalchemy import create_engine
 from sqlalchemy.orm import Session, sessionmaker
 from sqlalchemy.pool import NullPool
-from starlette.concurrency import run_in_threadpool
 from starlette.responses import Response
 
 from .entities import Base
@@ -65,29 +64,16 @@ def get_session(request: Request) -> Iterator[Session]:
     request.state.db_session = session
     try:
         yield session
+        session.commit()
     finally:
         session.close()  # close() discards (rolls back) anything uncommitted
 
 
 class CommitRoute(APIRoute):
-    """Commits the request's DB session BEFORE the response object reaches
-    the sender — never in dependency teardown, which runs after the response
-    is on the wire (finding F2). Streaming responses would need a rethink;
-    the app has none."""
+    """Route class every router declares; `get_session` owns the commit."""
 
     def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
-        original = super().get_route_handler()
-
-        async def commit_then_respond(request: Request) -> Response:
-            response = await original(request)
-            session: Session | None = getattr(request.state, "db_session", None)
-            if session is not None and response.status_code < 400:
-                # threadpool: a sync commit on the event loop would stall
-                # every in-flight request (same class as the webhook offload)
-                await run_in_threadpool(session.commit)
-            return response
-
-        return commit_then_respond
+        return super().get_route_handler()
 
 
 def init_db() -> None:
```

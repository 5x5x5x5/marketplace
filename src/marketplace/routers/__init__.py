"""The JSON API routers, one module per audience. `api.py` mounts them.

Routers are thin: parse the request, take identity from the auth
dependency, call one service (`marketplace.services`), return the object
for the response model. Every router is built with `route_class=CommitRoute`."""

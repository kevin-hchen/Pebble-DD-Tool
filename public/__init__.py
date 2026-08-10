"""The public service — a separate application from the internal Streamlit app.

Nothing in `app.py` or `pages/` is imported from this package, and nothing here
is imported by them. That separation is the security property: the internal
tool is not reachable from the internet because it is not mounted, rather than
because a check declines to serve it.
"""

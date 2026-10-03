"""Test package. Sets a throwaway SQLite DATABASE_URL before anything imports
the app, so even app/__init__.py's module-level create_app() stays hermetic
and never touches the real dev database."""
import os
import tempfile

if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(
        prefix="fitx-tests-default-", suffix=".db"
    ).replace("\\", "/")

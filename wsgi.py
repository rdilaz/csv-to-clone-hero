"""Production WSGI entry point.

Run with waitress or gunicorn (gunicorn is linux-only; this image also works
with waitress on Windows):

    python -m waitress --port 8000 wsgi:app
    gunicorn -w 1 -b 0.0.0.0:8000 wsgi:app

The single worker is deliberate: one global background job worker plus one web
process keeps the shared Enchor rate budget trivially safe, and the WSGI server
still serves many concurrent users (waitress/gunicorn threads).
"""

from csv_to_clonehero.server.web import create_app

app = create_app()

if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=8000, threads=8)
# Gunicorn settings. Each worker starts the monthly billing loop; a file lock
# makes sure only one of them actually runs it.


def post_fork(server, worker):
    import app

    app.start_scheduler()

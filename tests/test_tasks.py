import threading

from replicate import tasks


def test_run_in_thread_forwards_positional_and_keyword_arguments(monkeypatch):
    seen = {}
    done = threading.Event()
    monkeypatch.setattr(tasks, "GLib", None)  # callbacks run on the worker thread in tests

    def work(a, b, *, flag=False, label=""):
        seen.update(a=a, b=b, flag=flag, label=label)
        return "ok"

    results = []
    tasks.run_in_thread(work, 1, 2, flag=True, label="x", on_done=lambda r: (results.append(r), done.set()), name="t")
    assert done.wait(5)
    assert seen == {"a": 1, "b": 2, "flag": True, "label": "x"} and results == ["ok"]


def test_run_in_thread_reports_errors(monkeypatch):
    monkeypatch.setattr(tasks, "GLib", None)
    done = threading.Event()
    errors = []

    def boom(**kwargs):
        raise ValueError("no " + kwargs["thing"])

    tasks.run_in_thread(boom, thing="luck", on_error=lambda e: (errors.append(str(e)), done.set()))
    assert done.wait(5)
    assert errors == ["no luck"]

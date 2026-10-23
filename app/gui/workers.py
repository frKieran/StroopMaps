"""Background work for the window: every slow call (reading a
ROM, an export, an install) runs in a thread of its own, and everything it has to say reaches the Tk thread through one queue that the window
polls with after(). Tk is only ever touched from the Tk thread.

    tasks = Tasks(root)
    t = tasks.start('open', work_fn, arg1, on_done=f, on_error=g, on_progress=h)
        # work_fn(task, arg1) runs in a thread; it may call task.progress(x) and check task.cancelled
    t.cancel()                    # sets task.cancel_event (work_fn decides what stopping means)
    tasks.post(fn, *args)         # run fn(*args) on the Tk thread soon (from any thread)
"""
import queue
import sys
import threading
import time
import traceback


class Task:
    """A worker thread's job: its function, callbacks, cancel flag and outcome."""
    def __init__(self, tasks, name, fn, args, on_done, on_error, on_progress):
        self.tasks, self.name = tasks, name
        self.fn, self.args = fn, args
        self.on_done, self.on_error, self.on_progress = on_done, on_error, on_progress
        self.cancel_event = threading.Event()
        self.started = time.time()
        self.finished = None            # time.time() when the result reached the Tk thread
        self.result = None
        self.error = None
        self.thread = threading.Thread(target=self._run, name=f'gui-{name}', daemon=True)
        self.data = {}                  # what the work function wants the window to reach (a Job, a pid ...)

    @property
    def cancelled(self):
        """True once cancel() was called."""
        return self.cancel_event.is_set()

    @property
    def running(self):
        """True until the function returns or raises."""
        return self.finished is None

    def cancel(self):
        """Ask the job to stop (it checks cancelled at its own pace)."""
        self.cancel_event.set()

    def progress(self, value):
        """Report progress to the Tk thread's on_progress."""
        self.tasks.q.put(('progress', self, value))

    def _run(self):
        try:
            res = self.fn(self, *self.args)
        except BaseException as e:                      # noqa: BLE001 - reported to the window, never lost
            self.tasks.q.put(('error', self, (e, traceback.format_exc())))
        else:
            self.tasks.q.put(('done', self, res))


class Tasks:
    """The worker threads and the queue the Tk thread polls for their results."""
    POLL_MS = 40

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.live = {}                  # name -> Task (one per name at a time)
        self._after = root.after(self.POLL_MS, self._poll)
        self.closed = False

    def busy(self, name):
        """True while a job called `name` runs."""
        t = self.live.get(name)
        return t is not None and t.running

    def start(self, name, fn, *args, on_done=None, on_error=None, on_progress=None):
        """Run fn(task, *args) in a thread; the callbacks run on the Tk thread."""
        if self.busy(name):
            raise RuntimeError(f'a {name} task is already running')
        t = Task(self, name, fn, args, on_done, on_error, on_progress)
        self.live[name] = t
        t.thread.start()
        return t

    def post(self, fn, *args):
        """Call fn(*args) on the Tk thread."""
        self.q.put(('call', fn, args))

    def _poll(self):
        if self.closed:
            return
        try:
            self._drain()
        finally:                                        # whatever happened, keep polling
            if not self.closed:
                self._after = self.root.after(self.POLL_MS, self._poll)

    def _drain(self):
        try:
            for _ in range(500):                        # bounded: the window stays responsive under a flood
                kind, a, b = self.q.get_nowait()
                if kind == 'call':
                    self._safe(a, *b)
                    continue
                task = a
                if kind == 'progress':
                    if task.on_progress:
                        self._safe(task.on_progress, task, b)
                    continue
                task.finished = time.time()
                if self.live.get(task.name) is task:
                    del self.live[task.name]            # (a finished task's result is the caller's to keep)
                if kind == 'done':
                    task.result = b
                    if task.on_done:
                        self._safe(task.on_done, task, b)
                else:
                    task.error = b
                    if task.on_error:
                        self._safe(task.on_error, task, b[0], b[1])
        except queue.Empty:
            pass

    errors = []                 # (time, traceback) of callbacks that raised: shown nowhere else in a windowed exe

    @classmethod
    def _safe(cls, fn, *a):
        try:
            fn(*a)
        except Exception:                               # noqa: BLE001 - a view bug must not stop the polling
            tb = traceback.format_exc()
            cls.errors.append((time.time(), tb))
            if sys.stderr is not None:                  # None in the windowed exe
                sys.stderr.write(tb)

    def close(self):
        """Ask every running task to stop (the window is closing)."""
        self.closed = True
        for t in self.live.values():
            if t.running:
                t.cancel()
        try:
            self.root.after_cancel(self._after)
        except Exception:                               # noqa: BLE001
            pass

    def pump_until(self, cond, timeout, step=0.02):
        """Tests: run the Tk loop until cond() is true or timeout s pass; returns cond()."""
        t_end = time.time() + timeout
        while time.time() < t_end:
            self.root.update()
            if cond():
                return True
            time.sleep(step)
        return bool(cond())

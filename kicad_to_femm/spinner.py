"""
Simple progress spinner running on a separate thread.
"""
from time import sleep, perf_counter
import threading
from itertools import cycle


# Decorator to add a spinner to the execution of any function
def spinner(text):
    def decorator(function):
        def wrapper(*args, **kwargs):
            print(text)
            _start()
            start_time = perf_counter()
            function(*args, **kwargs)
            stop_time = perf_counter()
            _stop()
            print('{:0.3f}s'.format(stop_time - start_time))
        return wrapper
    return decorator

# Synchronization flags
_started = False  # set by the task runner
_stopped = True   # set by the task

cursor = cycle('|/-\\')


def _spinner_task():
    global _started, _stopped
    _stopped = False
    while _started:
        print(next(cursor), end='', flush=True)
        sleep(0.1)
        print('\r', end='', flush=True)

    _stopped = True


def _start():
    global _started, _stopped
    _started = False
    while not _stopped:
        pass
    _started = True
    threading.Thread(target=_spinner_task, daemon=True).start()


def _stop():
    global _started, _stopped
    _started = False
    while not _stopped:
        pass

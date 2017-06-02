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
            start()
            start_time = perf_counter()
            function(*args, **kwargs)
            stop_time = perf_counter()
            stop()
            print('{:0.3f}s'.format(stop_time - start_time))
        return wrapper
    return decorator

# Synchronization flags
started = False  # set by the task runner
stopped = True   # set by the task

cursor = cycle('|/-\\')


def spinner_task():
    global started, stopped
    stopped = False
    while started:
        print(next(cursor), end='', flush=True)
        sleep(0.1)
        print('\r', end='', flush=True)

    stopped = True


def start():
    global started, stopped
    started = False
    while not stopped:
        pass
    started = True
    threading.Thread(target=spinner_task, daemon=True).start()


def stop():
    global started, stopped
    started = False
    while not stopped:
        pass

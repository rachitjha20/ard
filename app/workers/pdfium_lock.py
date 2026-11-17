"""One lock for every use of PDFium.

PDFium is not thread-safe, and that applies to the whole library, not to one
document. Worker processes run a single task at a time, but in thread mode
(used by the tests) several tasks share a process, so all of them must take
this same lock.
"""

import threading

PDFIUM_LOCK = threading.RLock()

import ctypes
import sys

if sys.platform != "darwin":
    raise RuntimeError("Resource measurement requires macOS. Recorded reports can be read on any platform.")

class Rusage(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64) for name in (
            "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups",
            "pageins", "wired_size", "resident_size", "phys_footprint",
            "proc_start_abstime", "proc_exit_abstime",
        )
    ]


LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
LIBPROC.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
LIBPROC.proc_pid_rusage.restype = ctypes.c_int


class Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


TIMEBASE = Timebase()
LIBSYSTEM = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
LIBSYSTEM.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
if LIBSYSTEM.mach_timebase_info(ctypes.byref(TIMEBASE)):
    raise RuntimeError("mach_timebase_info failed")


def usage(pid):
    info = Rusage()
    if LIBPROC.proc_pid_rusage(pid, 0, ctypes.byref(info)):
        raise OSError(ctypes.get_errno(), "proc_pid_rusage failed")
    return {"user_ns": info.user_time * TIMEBASE.numer // TIMEBASE.denom,
            "system_ns": info.system_time * TIMEBASE.numer // TIMEBASE.denom,
            "rss_bytes": info.resident_size, "footprint_bytes": info.phys_footprint}

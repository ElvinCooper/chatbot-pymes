"""Parser PDF aislado. Límite de memoria y tiempo; salida de texto acotada."""

import json
import os
import sys


def limit_memory():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("per_process_time", ctypes.c_int64),
                ("per_job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t),
                ("processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", IO),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        job = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
        limits.process_memory = 256 * 1024 * 1024
        if (
            not job
            or not kernel.SetInformationJobObject(
                job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            )
            or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())
        ):
            raise RuntimeError("No se pudo aislar el parser")
        # El handle sigue abierto hasta terminar este proceso.
    else:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))


def extract(path: str) -> str:
    limit_memory()
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted or len(reader.pages) > 100:
        raise ValueError("PDF no permitido")
    parts, size = [], 0
    for page in reader.pages:
        part = page.extract_text() or ""
        size += len(part)
        if size > 300_000:
            raise ValueError("Demasiado texto")
        parts.append(part)
    return "\n".join(parts)


if __name__ == "__main__":
    try:
        text = extract(sys.argv[1])
        sys.stdout.buffer.write(json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"))
    except Exception:
        sys.stderr.write("No se pudo extraer el PDF de forma segura\n")
        raise SystemExit(1)

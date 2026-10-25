#!/usr/bin/env python3
"""The user's home directory, resolved from the OS rather than the environment.

ONE implementation, shared by every hook that needs a trustworthy home. Split
out of `preload-spine.py`, where the Windows half was first written, because
`enforce-journal-entry.py` needed the identical primitive and copying
forty lines of pinned ctypes signatures into a second file is how the two
drift apart.

WHY NOT `Path.home()` / `expanduser`. Both read the environment first: `$HOME`
on POSIX, `%USERPROFILE%` then `%HOMEDRIVE%%HOMEPATH%` on Windows. Every hook
here can be started by a session whose cwd is an untrusted repo, and a
direnv-loaded `.envrc`, a devcontainer, or a shell profile sets environment
variables trivially. The attack is never "change what your home directory
contains", it is "change where home POINTS" (reproduced before the fix
shipped). A hook that resolves a trust config, or an executable, through a
redirectable home hands the walked tree control of the thing meant to gate it.

POSIX takes the passwd entry. Windows takes `GetUserProfileDirectoryW`, which
reads `ProfileImagePath` for the calling token's SID and expands nothing.

`SHGetKnownFolderPath` is NOT an acceptable substitute and was the first
attempt at the Windows half: `FOLDERID_LocalAppData` resolves through the
`User Shell Folders` registry value `%USERPROFILE%\\AppData\\Local`, a
REG_EXPAND_SZ the API expands against the CALLING PROCESS's environment, so a
poisoned `%USERPROFILE%` moves it. That reopened the home redirect on a second platform,
and it measured clean only because the test poisoned to a path that did not
exist while `KF_FLAG_DEFAULT` verifies existence, which turned a live redirect
into a failed call.

Returns None when the home cannot be determined. Callers degrade to "no
trusted config found" rather than guessing a path: no candidate is a clean
no-op, a wrong candidate is a location an attacker chooses.
"""

import os

try:
    import pwd  # POSIX only; absent on Windows, a real host for these hooks.
except ImportError:
    pwd = None

# Resolved once per process. The Windows lookup pins six ctypes signatures and
# opens a token, and the answer cannot change mid-session. `_UNSET` rather than
# None as the miss marker, because None is itself a real, cacheable answer.
_UNSET = object()
_CACHED = _UNSET


def _windows_profile_dir(log=None):
    """The calling user's profile root on Windows, from the process token."""
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        userenv = ctypes.WinDLL("userenv", use_last_error=True)

        # Every signature is pinned deliberately. At ctypes defaults the
        # 64-bit HANDLE truncates to c_int and the call fails outright, which
        # is what made this approach look unavailable on the first attempt.
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        userenv.GetUserProfileDirectoryW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL

        TOKEN_QUERY = 0x0008
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         TOKEN_QUERY, ctypes.byref(token)):
            if log:
                log("agent_home: OpenProcessToken failed")
            return None
        try:
            size = wintypes.DWORD(0)
            # The first call always "fails" with ERROR_INSUFFICIENT_BUFFER and
            # sets the required length; that is the documented sizing call.
            userenv.GetUserProfileDirectoryW(token, None, ctypes.byref(size))
            if not size.value:
                if log:
                    log("agent_home: GetUserProfileDirectoryW returned no size")
                return None
            buf = ctypes.create_unicode_buffer(size.value)
            if not userenv.GetUserProfileDirectoryW(token, buf,
                                                    ctypes.byref(size)):
                if log:
                    log("agent_home: GetUserProfileDirectoryW failed")
                return None
            return buf.value or None
        finally:
            kernel32.CloseHandle(token)
    except Exception as exc:
        # Fail-open like every hook here, but never SILENTLY: a host that
        # cannot determine where to look must not be reported as a host with
        # nothing installed. Conflating those is the bug this exists to end.
        if log:
            log(f"agent_home: Windows profile lookup unavailable ({exc!r})")
        return None


def _is_truly_absolute(value):
    """`os.path.isabs`, minus the drive-relative shapes it accepts on Windows.

    On Windows before Python 3.13, `ntpath.isabs("/x")` and `isabs("\\\\x")` are
    True even though both are DRIVE-RELATIVE: they resolve against whatever
    drive the process is on, which for these hooks is the walked tree's. That
    is exactly the cwd-derived resolution the absolute check exists to refuse,
    so an override of `/sneaky` would have been honoured on the pinned 3.12 CI
    while being refused on 3.14. Requiring a drive letter closes it on every
    version."""
    if not value:
        return False
    if os.name == "nt":
        return bool(os.path.splitdrive(value)[0]) and os.path.isabs(value)
    return os.path.isabs(value)


def env_override_or_home(env_name, *parts, log=None):
    """An ABSOLUTE `$env_name` outright, else `<trusted home>/<parts>`, else None.

    One implementation of the override shape this plugin uses in two places
    (this and vardemem's `$VARDEMEM_TRUST_FILE`), so the drive-relative defect
    above is fixed once rather than per copy."""
    override = os.environ.get(env_name, "").strip()
    if _is_truly_absolute(override):
        return override
    home = trusted_home(log)
    if home is None:
        return None
    return os.path.join(home, *parts)


def trusted_home(log=None):
    """This user's home directory as a string, or None. Never env-derived.

    `log` is an optional one-argument callable for the failure paths; hooks
    pass their own logger so a host that cannot resolve a home says so.
    """
    global _CACHED
    if _CACHED is not _UNSET:
        return _CACHED
    if os.name == "nt":
        _CACHED = _windows_profile_dir(log)
        return _CACHED
    try:
        _CACHED = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError, AttributeError) as exc:
        # AttributeError: `pwd.getpwuid` is looked up before `os.getuid()` is
        # called, so a None `pwd` (import failed) raises here first.
        # Logged for the same reason the Windows branch logs: "could not
        # determine a home" and "nothing is configured" are different states,
        # and a caller that cannot tell them apart reports the wrong one.
        if log:
            log(f"agent_home: no passwd entry for this uid ({exc!r})")
        _CACHED = None
    return _CACHED


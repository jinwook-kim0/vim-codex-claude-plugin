"""Non-mutating write probe run by Codex command/exec in its current sandbox."""

# Passed as Python -c code so a custom profile need not grant read access to
# the plugin's source directory. Opening an existing file without O_TRUNC
# checks kernel-enforced write access without changing its bytes or timestamp.
WRITE_PROBE = r'''
import errno, json, os, sys
path = os.path.realpath(sys.argv[1])
try:
    if os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    else:
        fd = os.open(os.path.dirname(path), os.O_WRONLY | os.O_TMPFILE | os.O_CLOEXEC, 0o600)
    os.close(fd)
    print(json.dumps({"allowed": True, "path": path}))
except OSError as error:
    print(json.dumps({"allowed": False, "path": path, "errno": error.errno,
                      "permission_denied": error.errno in (errno.EACCES, errno.EPERM, errno.EROFS),
                      "message": str(error)}))
'''

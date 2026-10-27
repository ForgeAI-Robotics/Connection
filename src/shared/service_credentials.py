"""Read service credentials without storing them in configuration or ledgers."""
import os
import stat


def service_token(env_name):
    """An explicitly configured private file takes precedence, with no fallback."""
    path = os.environ.get(env_name + '_FILE', '')
    if path:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'r', encoding='ascii') as handle:
                mode = os.fstat(handle.fileno()).st_mode
                if not stat.S_ISREG(mode) or mode & 0o077:
                    raise ValueError('invalid credential file')
                token = handle.read(4097).strip()
        except (OSError, UnicodeError, ValueError):
            raise ValueError('服务凭据文件不可读或不是私有普通文件') from None
        if not token:
            raise ValueError('服务凭据文件为空')
    else:
        token = os.environ.get(env_name, '')
    if len(token) > 4096 or any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError('服务凭据格式无效')
    return token

import sys

def safe_print(*args, **kwargs):
    """
    In an toan ra console, tu dong xu ly UnicodeEncodeError tren Windows (cp1258/cp1252/charmap).
    """
    try:
        print(*args, **kwargs)
    except (UnicodeEncodeError, Exception):
        try:
            encoding = getattr(sys.stdout, 'encoding', 'ascii') or 'ascii'
            clean_args = []
            for a in args:
                if isinstance(a, str):
                    clean_args.append(a.encode(encoding, errors='replace').decode(encoding, errors='replace'))
                else:
                    clean_args.append(str(a).encode(encoding, errors='replace').decode(encoding, errors='replace'))
            print(*clean_args, **kwargs)
        except Exception:
            pass

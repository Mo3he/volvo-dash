"""Container health check: is the web server answering."""

import os
import ssl
import sys
import urllib.request

from volvodash.tls import enabled

tls = enabled()
url = f"{'https' if tls else 'http'}://127.0.0.1:{os.environ.get('PORT', '8000')}/api/session"
try:
    # Our own certificate is not one anybody vouches for, and that's fine here.
    urllib.request.urlopen(url, timeout=4, context=ssl._create_unverified_context() if tls else None)
except Exception as e:  # noqa: BLE001
    print(e, file=sys.stderr)
    sys.exit(1)

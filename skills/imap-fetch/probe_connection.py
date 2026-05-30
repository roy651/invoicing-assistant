"""Quick IMAP connection probe — just connects, authenticates, and reports inbox stats."""

import os
import imapclient
from pathlib import Path

# Walk up to find .env
here = Path(__file__).resolve().parent
for candidate in [here, here.parent, here.parent.parent, here.parent.parent.parent]:
    env_file = candidate / ".env"
    if env_file.exists():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
        print(f"Loaded .env from {env_file}")
        break

host = os.environ.get("IMAP_HOST", "")
port = int(os.environ.get("IMAP_PORT", "993"))
user = os.environ.get("IMAP_USER", "")
password = os.environ.get("IMAP_APP_PASSWORD", "")
print(
    f"IMAP_HOST={host!r}  IMAP_PORT={port}  IMAP_USER={user!r}  APP_PASSWORD={'***' if password else '(not set)'}"
)

print("Connecting...")
server = imapclient.IMAPClient(host, port=port, ssl=True)
print("Logging in...")
server.login(user, password)
print("Login OK")
info = server.select_folder("INBOX", readonly=True)
print(f"INBOX: EXISTS={info.get(b'EXISTS', '?')}  UIDNEXT={info.get(b'UIDNEXT', '?')}")
server.logout()
print("Done — connection, auth, and read-only SELECT all succeeded.")

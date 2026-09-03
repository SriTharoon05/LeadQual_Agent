"""Create or rotate an admin password using bcrypt."""
import getpass

import bcrypt

from app.database import db_session
from app.models import AdminUser


def main() -> None:
    username = input("Admin username: ").strip()
    password = getpass.getpass("Admin password: ")
    if not username or len(password) < 12 or len(password.encode("utf-8")) > 72:
        raise SystemExit("Username is required and password must be 12–72 UTF-8 bytes.")
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    with db_session() as db:
        user = db.query(AdminUser).filter(AdminUser.username == username).first()
        if user:
            user.password = password_hash
        else:
            db.add(AdminUser(username=username, password=password_hash))
    print(f"Admin '{username}' is ready.")


if __name__ == "__main__":
    main()

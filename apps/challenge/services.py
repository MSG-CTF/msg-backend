import hashlib

from django.contrib.auth.hashers import check_password


def hash_flag(flag):
    return hashlib.sha256(flag.encode("utf-8")).hexdigest()


def is_correct_flag(flag, flag_hash):
    try:
        if check_password(flag, flag_hash):
            return True
    except ValueError:
        pass
    if hash_flag(flag) == flag_hash:
        return True
    return False

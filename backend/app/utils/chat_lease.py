"""Renewable conversation ownership without reserving a SQL pool connection."""
import logging
import threading
from functools import lru_cache
from uuid import uuid4

import redis

from app.config import settings

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120


@lru_cache(maxsize=1)
def _client():
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True,
        socket_connect_timeout=3, socket_timeout=3, health_check_interval=30)


class ConversationLease:
    def __init__(self, conversation_id):
        self.key = "ql:chat:lease:" + conversation_id
        self.token = str(uuid4())
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = None

    def acquire(self):
        if not _client().set(self.key, self.token, nx=True, ex=LEASE_SECONDS):
            raise TimeoutError("A reply is already being prepared for this conversation")
        self.thread = threading.Thread(target=self._renew, name="chat-lease", daemon=True)
        try:
            self.thread.start()
        except BaseException:
            self.release()
            raise

    def _renew(self):
        while not self.stop.wait(25):
            try:
                self.assert_owned()
            except Exception:
                self.lost.set()
                return

    def assert_owned(self, *_args):
        if self.lost.is_set():
            raise TimeoutError("Conversation ownership was interrupted; retry this request")
        valid = _client().eval("""
            if redis.call('GET',KEYS[1]) ~= ARGV[1] then return 0 end
            redis.call('EXPIRE',KEYS[1],ARGV[2]); return 1
        """, 1, self.key, self.token, LEASE_SECONDS)
        if not valid:
            self.lost.set()
            raise TimeoutError("Conversation ownership expired; retry this request")

    def release(self):
        self.stop.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=4)
        try:
            _client().eval("""
                if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) end
                return 0
            """, 1, self.key, self.token)
        except Exception:
            logger.warning("Could not release a conversation lease; it will expire automatically")

from flask_login import UserMixin
from app import db, rd
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask import current_app, g
from random import randint
from itsdangerous import TimedJSONWebSignatureSerializer as Serializer
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy import event
from time import time
from app.utils.logger import logfuncall
from app.utils import to_http_date

user_follows = db.Table('user_follows',
                        db.Column('follower_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
                        db.Column('followed_id', db.Integer, db.ForeignKey('users.id'), primary_key=True))


def defaultUsername(context):
    return '用户_' + str(int(time() * 10000000 % 10000000000000000))


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, nullable=False, primary_key=True)
    email = db.Column(db.String(64), unique=True, index=True)
    # Todo: 不能为空, 两端的字符不能为空格
    username = db.Column(db.String(32), nullable=False, unique=True,
                         index=True, default=defaultUsername)
    password_hash = db.Column(db.String(128))
    avatar = db.Column(db.String(64), nullable=False,
                       default='default_avatar.jpg')
    self_intro = db.Column(db.String(40), default='')
    # 用户的性别，值为1时是男性，值为2时是女性，值为0时是未知
    gender = db.Column(db.Integer, default=0)
    # user status
    member_since = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)

    """ Relationships """
    # 动态
    statuses = db.relationship('Status', backref='user', lazy='dynamic',
                               cascade='all, delete-orphan')
    status_replies = db.relationship('StatusReply', backref='user',
                                     lazy='dynamic', cascade='all, delete-orphan')
    # 用户关注
    followed = db.relationship(
        'User', secondary=user_follows,
        primaryjoin=(user_follows.c.follower_id == id),
        secondaryjoin=(user_follows.c.followed_id == id),
        backref=db.backref('followers', lazy='dynamic'), lazy='dynamic')
    # 团体
    group_memberships = db.relationship("GroupMembership",
                                        back_populates="user", cascade='all, delete-orphan', lazy='dynamic')
    # 添加直接访问方式, 可以直接通过u.groups访问到用户所在的团体
    groups = association_proxy("group_memberships", "group")
    # 二手
    sales = db.relationship('Sale', backref='user', lazy='dynamic')
    sale_comments = db.relationship('SaleComment', backref='user',
                                    lazy='dynamic')
    # private messages
    messages = db.relationship('Message', backref='user', lazy='dynamic')

    def generate_auth_token(self, expiration):
        s = Serializer('auth' + current_app.config['SECRET_KEY'],
                       expires_in=expiration)
        return s.dumps({'id': self.id}).decode('ascii')

    @staticmethod
    def verify_auth_token(token):
        import app.cache as Cache
        import app.cache.redis_keys as KEYS
        """ Get current User from token """
        token_key = KEYS.user_token.format(token)
        data = rd.get(token_key)
        if data is not None:
            return Cache.get_user(data.decode())
        s = Serializer('auth' + current_app.config['SECRET_KEY'])
        try:
            data = s.loads(token)
            rd.set(token_key, data['id'], KEYS.user_token_expire)
            return Cache.get_user(data['id'])
        except:
            return None

    def verify_password(self, password):
        return check_password_hash(self.password_hash, password)

    @staticmethod
    def process_json(json_user):
        import app.cache as Cache
        id = json_user['id']
        t = Cache.is_user_followed_by(id, g.user.id) if \
            hasattr(g, 'user') else False
        json_user['followed_by_me'] = t
        json_user['last_seen'] = to_http_date(
            json_user['last_seen'])
        json_user['member_since'] = to_http_date(
            json_user['member_since'])
        return json_user

    @logfuncall
    def to_json(self, cache=False):
        image_server = current_app.config['IMAGE_SERVER']
        # NOTE: keep json_user without nested dict in order to
        # perfectly store it to redis hast data type
        json_user = {
            'id': self.id,
            'username': self.username,
            'avatar': image_server + self.avatar,
            'self_intro': self.self_intro,
            'gender': self.gender,
            'member_since': self.member_since.timestamp(),
            'last_seen': self.last_seen.timestamp(),
            'groups_enrolled': self.group_memberships.count(),
            'followed': self.followed.count(),
            'followers': self.followers.count(),
        }
        if not cache:
            return User.process_json(json_user)
        # `followed_by_me` is g.user relevant, which
        # is set in app.cache.users
        return json_user

    def __repr__(self):
        return '<User: %r>' % self.username

    @property
    def password(self):
        raise AttributeError('password is not a readable attribute')

    @password.setter
    def password(self, password):
        self.password_hash = generate_password_hash(password)


def _clear_redis_cache(instance: User):
    import app.cache.redis_keys as Keys
    id = instance.id
    keys_to_remove = [
        Keys.user.format(id),
        Keys.user_token.format(id),
        Keys.user_json.format(id),
        Keys.user_followers.format(id),
    ]
    rd.delete(*keys_to_remove)


@logfuncall
@event.listens_for(User, "after_insert")
@event.listens_for(User, "after_update")
def user_updated(mapper, connection, target):
    _clear_redis_cache(target)
    # TODO: Using Cache.cache_user_json() to reduce a sql query


@logfuncall
@event.listens_for(User, "after_delete")
def user_deleted(mapper, connection, target):
    _clear_reids_cache(target)


def randomVerificationCode(context):
    return str(randint(100000, 999999))


class WaitingUser(db.Model):
    __classname = 'waiting_users'

    email = db.Column(db.String(64), primary_key=True)
    password_hash = db.Column(db.String(128))

    verification_time = db.Column(db.DateTime, default=datetime.utcnow)
    verification_code = db.Column(db.String(6), default=randomVerificationCode)

    @property
    def password(self):
        raise AttributeError('password is not a readable attribute')

    @password.setter
    def password(self, password):
        self.password_hash = generate_password_hash(password)

    @staticmethod
    def verify(email, code):
        wu = WaitingUser.query.get(email)
        if wu is None:
            return None
        if wu.verification_code != code:
            return None
        now = datetime.utcnow()
        diff = now - wu.verification_time
        minutes = divmod(diff.days * 86400 + diff.seconds, 60)[0]
        if minutes >= 15:
            return None
        return wu

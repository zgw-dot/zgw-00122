import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
from models import db, init_db


@pytest.fixture
def app():
    """Flask app 实例，使用内存 SQLite"""
    flask_app.config["TESTING"] = True
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    flask_app.config["WTF_CSRF_ENABLED"] = False
    flask_app.config["UPLOAD_FOLDER"] = tempfile.gettempdir()
    init_db(flask_app)
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    """测试客户端"""
    return app.test_client()


@pytest.fixture
def sample_dir():
    """样例文件目录"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample")

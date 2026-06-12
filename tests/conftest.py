import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from models import db


@pytest.fixture
def app():
    """每个用例都创建全新 Flask 实例 + 独立内存 SQLite，彻底隔离"""
    _app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": tempfile.gettempdir(),
    })
    with _app.app_context():
        db.create_all()
        yield _app
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

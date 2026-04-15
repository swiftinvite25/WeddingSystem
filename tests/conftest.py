# tests/conftest.py
import sys
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# --- IMPORTANT: PATH MODIFICATION FIRST ---
# Get the path to the 'WeddingSystem' directory (the parent of 'tests')
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
# Add this path to the sys.path (Python's search path for modules)
sys.path.insert(0, project_root)

# --- APPLICATION IMPORTS AFTER PATH IS SET ---
# Import 'app' from app_web.py
from app import app

# Import database components directly that you need by name
# OR, import the models module itself if you want to reference its globals via models.
# For global variables like _engine and _SessionLocal, importing the module is safer
# because you want to modify the global variable *in that module*, not a local copy.
import models # <--- THIS IS THE CRITICAL NEW/MISSED LINE

# You can still import Base, Guest, init_db directly if you prefer,
# as these aren't directly modified as global variables in the fixture.
from models import Base, Guest, init_db

# --- Fixture for SQLAlchemy Database Session ---
@pytest.fixture(scope='function')
def db_session():
    engine = create_engine('sqlite:///:memory:')

    # Temporarily override the global engine and SessionLocal in models.py
    # Access the _SessionLocal and _engine from the models module
    original_SessionLocal = models._SessionLocal # Correct: reference via models module
    original_engine = models._engine             # Correct: reference via models module

    # Set the global variables in the models module to the test engine/sessionmaker
    models._engine = engine                      # Correct: assign to models module's global
    models._SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) # Correct: assign to models module's global

    Base.metadata.create_all(models._engine)     # Create tables with the test engine (use models._engine)

    session = models._SessionLocal()             # Get session from the test SessionLocal (use models._SessionLocal)

    yield session

    session.close()
    Base.metadata.drop_all(models._engine)       # Drop tables after the test (use models._engine)

    # Restore original _SessionLocal and _engine after the test
    models._SessionLocal = original_SessionLocal # Correct: restore models module's global
    models._engine = original_engine             # Correct: restore models module's global


# --- Fixture for Flask Test Client ---
@pytest.fixture
def client(db_session):
    # Configure app for testing
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'a_secret_key_for_testing'
    app.config['WTF_CSRF_ENABLED'] = False

    # IMPORTANT: Ensure the app's DB_URI is set to the in-memory DB for tests
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'

    # Push an app context to ensure app.config is available for init_db
    with app.app_context():
        # Call init_db using the app context and the test configuration
        init_db(app) # This will initialize models._engine and models._SessionLocal using app.config

        with app.test_client() as client:
            yield client
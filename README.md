# Django API Project

This project is a Django-based API with token authentication. Follow the instructions below to set up and run the application locally.

## Prerequisites

- Python 3.x
- pip (Python package manager)
- Virtualenv (optional but recommended)

## Setup Instructions

### 1. Clone the Repository

First, clone the repository to your local machine:

```bash
git clone <repository-url>
cd <repository-directory>
```

### 2. Create a Virtual Environment

It is recommended to use a virtual environment to manage dependencies:

```bash
python -m venv venv
```

Activate the virtual environment:

- On Windows:
  ```bash
  venv\Scripts\activate
  ```
- On macOS and Linux:
  ```bash
  source venv/bin/activate
  ```

### 3. Install Dependencies

Install the required Python packages using pip:

```bash
pip install -r requirements.txt
```

### 4. Apply Migrations

Run the following command to apply database migrations:

```bash
python manage.py migrate
```

### 5. Create a Superuser

Create a superuser account to access the Django admin interface:

```bash
python manage.py createsuperuser
```

Follow the prompts to set up the superuser credentials.

### 6. Run the Development Server

Start the Django development server:

```bash
python manage.py runserver
```

The application will be accessible at `http://127.0.0.1:8000/`.

### 7. Access the API

You can access the token authentication endpoint at:

```
http://127.0.0.1:8000/api/auth/token/
```

Use this endpoint to obtain an authentication token by providing valid user credentials.

## Additional Information

- To deactivate the virtual environment, simply run `deactivate`.
- Ensure you have the correct permissions and configurations for your database and environment.

## Troubleshooting

If you encounter any issues, ensure that all dependencies are installed correctly and that the virtual environment is activated. Check the Django documentation for further assistance.

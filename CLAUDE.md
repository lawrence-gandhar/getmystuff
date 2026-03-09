# CLAUDE.md
Project: GetMyStuff
Authoritative Development Guide for AI Coding Assistants

This file defines the engineering rules, architecture, and expectations for AI assistants (Claude, Cursor, etc.) when generating or modifying code in this repository.

AI MUST follow these rules strictly.


--------------------------------------------------
PROJECT OVERVIEW
--------------------------------------------------

GetMyStuff is an enterprise-grade AI-powered analytics platform that allows businesses to connect to multiple data sources and extract insights using natural language.

The system enables organizations to talk to their data without requiring dashboards, SQL knowledge, or engineering expertise.

Core capabilities include:

• Multi-database connectivity  
• Natural language querying  
• AI-driven analytics  
• File ingestion pipelines  
• High performance data processing  
• Enterprise-grade validation and error handling


--------------------------------------------------
TECHNOLOGY STACK
--------------------------------------------------

Backend
• Python 3.11+
• Litestar

Frontend
• HTMX
• Bootstrap 5
• HTML5
• CSS3

Databases
• PostgreSQL
• MySQL
• MongoDB

Data Processing
• Pandas
• PyArrow


--------------------------------------------------
CRITICAL DEVELOPMENT RULES
--------------------------------------------------

These rules are STRICT and must always be followed.

1. Always produce **enterprise-grade production code**.

2. **No silent failures are allowed.**
   Every failure must raise an exception with a clear human readable message.

3. Every route must return clear responses for:
   - success
   - validation failure
   - system failure

4. **All database CRUD operations MUST go through `db_utils.py`.**

5. Business logic MUST NEVER exist inside routes.

6. Business logic must be placed in:
   services/


7. API / web routes must exist only in:
   routes/


8. HTMX must be used for partial page updates when appropriate.

9. Templates must be separated by feature.

10. Do not mix responsibilities across files.

11. Always follow **DRY principle**.

12. Follow **PEP8 and Python best practices**.

13. All user input must be validated both:

• Frontend validation  
• Backend validation

14. Never allow unsanitized user input to reach service layer.

15. Templates must maintain a **consistent UI style**.

16. Always return **human readable error messages**.

17. Never expose raw stack traces to users.

18. Log errors internally when needed.


--------------------------------------------------
PROJECT STRUCTURE
--------------------------------------------------

The project structure must follow this pattern.

project-root/
    app/
    ├── routes/
    │ ├── dashboard.py
    │ ├── datasources.py
    │ ├── auth.py
    │
    ├── services/
    │ ├── datasource_service.py
    │ ├── auth_service.py
    │
    ├── utils/
    │ ├── db_utils.py
    │ ├── validators.py
    │ ├── exceptions.py
    │
    ├── models/
    │ ├── datasource.py
    │ ├── user.py
    │
    ├── templates/
    │ ├── base.html
    │ ├── dashboard/
    │ ├── datasource/
    │ ├── auth/
    │
    ├── static/
    │ ├── css/
    │ ├── js/


Rules:

• Routes handle HTTP requests only  
• Services contain business logic  
• Utils contain helpers  
• Templates contain only UI logic


--------------------------------------------------
DATABASE RULES
--------------------------------------------------

All CRUD operations must go through:
    app/db/db_utils.py

Never directly execute SQL queries inside routes or services.

`db_utils.py` should contain reusable functions such as:
    insert_record()
    update_record()
    delete_record()
    fetch_one()
    fetch_all()
    bulk_insert()


All database errors must be wrapped into custom exceptions.


--------------------------------------------------
CUSTOM EXCEPTIONS
--------------------------------------------------

All application errors must use custom exceptions.

Examples:
    ValidationError
    DatabaseError
    AuthenticationError
    AuthorizationError
    ResourceNotFoundError
    ServiceError


Example pattern:
    raise ValidationError("Datasource name cannot be empty")


--------------------------------------------------
SERVICE LAYER RULES
--------------------------------------------------

Services contain business logic.

Example:
    services/datasource_service.py


Responsibilities:

• validation
• orchestration
• database interaction via db_utils
• business rules

Services must NEVER:

• return HTML
• handle HTTP requests
• access request objects

They must return structured data.


--------------------------------------------------
ROUTE LAYER RULES
--------------------------------------------------

Routes must:

• accept request
• validate request
• call service
• return response

Routes must NOT contain business logic.

Example:
    routes/datasources.py


Route flow:
• validate input
• sanitize input
• call service
• return template or JSON


--------------------------------------------------
HTMX USAGE
--------------------------------------------------

HTMX should be used for dynamic UI updates such as:

• loading tables
• creating datasources
• updating records
• deleting records
• file upload status

Examples:
• hx-post
• hx-get
• hx-target
• hx-swap


All HTMX responses must return partial templates.


--------------------------------------------------
FRONTEND VALIDATION
--------------------------------------------------

All forms must include client-side validation.

Examples:

• required fields
• file type validation
• max file size
• correct formats

Example:
    <input required type="text" />


--------------------------------------------------
BACKEND VALIDATION
--------------------------------------------------

Never trust frontend validation.

Backend must validate:

• empty values
• invalid formats
• invalid database credentials
• unsupported file types

Example:
    if not datasource_name:
        raise ValidationError("Datasource name is required")



--------------------------------------------------
FILE INGESTION RULES
--------------------------------------------------

Supported file types:

• CSV
• XLSX
• JSON
• Parquet
• Avro

File ingestion must:

• validate file size
• validate file type
• process in chunks
• use Pandas or PyArrow


--------------------------------------------------
CODE STYLE
--------------------------------------------------

Follow PEP8.

Use:

• type hints
• docstrings
• clear variable names

Example:
    def create_datasource(
        name: str,
        connection_string: str
        ) -> dict:


Avoid:

• deeply nested code
• duplicate logic
• magic values


--------------------------------------------------
SECURITY RULES
--------------------------------------------------

Always enforce:

• input sanitization
• SQL injection prevention
• file upload restrictions
• safe database connections


--------------------------------------------------
TEMPLATE RULES
--------------------------------------------------

Templates must:

• extend base.html
• use Bootstrap 5
• maintain consistent styling
• avoid inline scripts when possible

Structure example:
    templates/
        datasource/
            create.html
            list.html
            partials/
                table.html



--------------------------------------------------
ERROR RESPONSE FORMAT
--------------------------------------------------

User errors must return readable messages.

Example:
    {
        "status": "error",
        "message": "Datasource connection failed. Please verify credentials."
    }


Success example:
    {
        "status": "success",
        "message": "Datasource created successfully."
    }



--------------------------------------------------
WHEN CLAUDE GENERATES CODE
--------------------------------------------------

Claude must always:

1. Respect the architecture.
2. Use services for business logic.
3. Use db_utils for database calls.
4. Add validation.
5. Add proper exception handling.
6. Follow DRY principle.
7. Keep code simple and readable.
8. Use HTMX when needed.
9. Generate consistent Bootstrap UI.


--------------------------------------------------
IMPORTANT FINAL RULE
--------------------------------------------------

If existing project patterns conflict with generated code,
Claude MUST follow the existing project pattern.


Never introduce a different architecture.

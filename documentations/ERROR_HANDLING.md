# ERROR_HANDLING.md

Enterprise error handling rules.

---

# Philosophy

Errors must always be:

* explicit
* readable
* logged
* recoverable

Silent failures are forbidden.

---

# Error Types

ValidationError
DatabaseError
AuthenticationError
AuthorizationError
ResourceNotFoundError
ServiceError

---

# Example Validation Error

```
if not datasource_name:
    raise ValidationError("Datasource name cannot be empty")
```

---

# Example Database Error

```
try:
    db.insert(...)
except Exception as e:
    raise DatabaseError("Failed to create datasource")
```

---

# User Response Format

Error response:

```
{
 "status": "error",
 "message": "Datasource connection failed."
}
```

Success response:

```
{
 "status": "success",
 "message": "Datasource created successfully."
}
```

---

# Logging

All internal errors must be logged.

Never expose stack traces to users.

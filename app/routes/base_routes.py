from litestar import Router, get, post

@post("/login")
async def login() -> dict:
    return {"message": "Login Logic"}

@post("/logout")
async def logout() -> dict:
    return {"message": "Logged Out"}


auth_router = Router(
    path="/auth",   
    route_handlers=[
        login, 
        logout
    ],
)
from litestar import (
    get, 
    post, 
    Controller
)

from litestar.connection import Request

from litestar.response import Template, Redirect

from app.db.auth import (
    authenticate_user,
    create_access_token,
    create_refresh_token,
    decode_token,

)

from sqlalchemy.ext.asyncio import AsyncSession

from litestar.exceptions import HTTPException

class AuthController(Controller):
    path = "/auth"

    @get("/login")
    async def login_page(self) -> Template:
        return Template(
            template_name="base/login.htm",
            context={"title": "Login"},
        )

    @post("/login")
    async def login(self, request: Request, db: AsyncSession) -> Redirect:
        form = await request.form()
        email = form.get("email")
        password = form.get("password")

        user = await authenticate_user(db, email, password)

        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        access_token = create_access_token(str(user.uuid))
        refresh_token = create_refresh_token(str(user.uuid))

        response = Redirect(path="/user/dashboard")

        response.set_cookie("access_token", access_token, httponly=True)
        response.set_cookie("refresh_token", refresh_token, httponly=True)

        return response

    @post("/refresh")
    async def refresh(self, request: Request, db: AsyncSession) -> Redirect:
        refresh_token = request.cookies.get("refresh_token")

        if not refresh_token:
            raise HTTPException(status_code=401)

        payload = decode_token(refresh_token)

        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401)

        user_uuid = payload.get("sub")

        new_access_token = create_access_token(user_uuid)

        response = Redirect(path="/dashboard")
        response.set_cookie("access_token", new_access_token, httponly=True)

        return response

    @post("/logout")
    async def logout(self) -> Redirect:
        response = Redirect(path="/auth/login")
        response.delete_cookie("access_token")
        response.delete_cookie("refresh_token")
        return response
from fastapi import FastAPI, Request, Form, Response, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import uuid
import os
from dotenv import load_dotenv
import asyncio
from api_analytics.fastapi import Analytics
from starlette.middleware.trustedhost import TrustedHostMiddleware
import functools
from typing import TypeVar, Callable, Any
from ingest import analyze_codebase

MAX_DISPLAY_SIZE = 300000

T = TypeVar("T")
def async_timeout(seconds: int = 10):
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            return await asyncio.wait_for(func(*args, **kwargs), timeout=seconds)
        return wrapper
    return decorator

# Load environment variables
load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(Analytics, api_key=os.getenv('API_ANALYTICS_KEY'))
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["gitingest.com", "*.gitingest.com", "gitdigest.dev", "localhost"])
templates = Jinja2Templates(directory="templates")



@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    print("read_root")
    return templates.TemplateResponse(
        "index.html", {
            "request": request, 
            "result": None, 
            "summary": "", 
            "tree": "", 
            "content": "",
        }
    )


@app.post("/", response_class=HTMLResponse)
async def process_input(request: Request, input_text: str = Form(...)):
    try:
        summary, tree, content = await process_input(input_text)
    except Exception as e:
        return templates.TemplateResponse(
            "index.html", 
            {
                "request": request, 
                "result": None, 
                "summary": "", 
                "tree": "", 
                "content": "",
                "error_message": f"Error processing repository: {e}"
            }
        )
    
    ingest_id = str(uuid.uuid4())
    with open(f"../tmp/ingest-{ingest_id}.txt", "w") as f:
        f.write(f"Summary:\n{summary}\n\nFile Tree:\n{tree}\n\nDetailed Content:\n{content}")
    
    if len(content) > MAX_DISPLAY_SIZE:
        content = f"(Files content cropped to {int(MAX_DISPLAY_SIZE/1000)}k characters, download full ingest to see more)\n" + content[:MAX_DISPLAY_SIZE]
        
    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request, 
            "summary": summary,
            "result": True, 
            "tree": tree, 
            "content": content,
            "error_message": None,
            "ingest_id": ingest_id
        }
    )

@app.get("/download/{ingest_id}")
async def download_ingest(ingest_id: str):
    try:
        with open(f"../tmp/ingest-{ingest_id}.txt", "r") as f:
            content = f.read()
        return Response(
            content=content,
            media_type="text/plain",
            headers={
                "Content-Disposition": f"attachment; filename=gitingest-{ingest_id[:8]}.txt"
            }
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Digest not found")

    
def get_repo_id(repo_url: str) -> str:
    if not repo_url.startswith("https://github.com/"):
        raise ValueError("Invalid GitHub URL. Please provide a valid GitHub repository URL.")
    repo_url = repo_url.split(" ")[0]
    
    id = repo_url.replace("https://github.com/", "").replace("/", "-")
    return id

@async_timeout(20)
async def clone_repo(repo_url: str, id: str) -> str:

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--depth=1",
            "--single-branch",
            repo_url,
            f"../tmp/{id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        stdout, stderr = await proc.communicate()
        
        return id
    except Exception as e:
        print(f"Clone failed with exception: {str(e)}")
        return f"Error cloning repository: {str(e)}"

@async_timeout(10)  # Longer timeout for processing
async def process_repo(
    repo_id: str,
):
    return analyze_codebase(f"../tmp/{repo_id}")

def delete_repo(repo_id: str):

    os.system(f"rm -drf ../tmp/{repo_id}")
    os.system(f"rm -f ../tmp/ingest-{repo_id}")

async def process_input(text: str) -> str:
    if not text.startswith("https://github.com/"):
        return "Invalid GitHub URL. Please provide a valid GitHub repository URL."
    
    repo_id = get_repo_id(text)
    if not repo_id:
        return "Invalid GitHub URL. Please provide a valid GitHub repository URL."
    
    delete_repo(repo_id)
    await clone_repo(text, repo_id)
    if not repo_id:
        return "Repository clone failed or timed out after 10 seconds."
        
    result = await process_repo(repo_id)
    delete_repo(repo_id)
    
    if not result:
        return "Repository processing failed or timed out after 30 seconds."
        
    return result

@app.get("/api", response_class=HTMLResponse)
async def api_docs(request: Request):
    return templates.TemplateResponse(
        "api.html", {"request": request}
    )

@app.get('/favicon.ico')
async def favicon():
    return FileResponse('static/favicon.ico')



def reconstruct_github_url(full_path: str) -> str:
    path_parts = full_path.split('/')
    
    
    # Reconstruct the GitHub URL
    github_url = f"https://github.com/{path_parts[0]}/{path_parts[1]}"
    return github_url

@app.get("/{full_path:path}")
async def catch_all(request: Request, full_path: str):
    try:
        github_url = reconstruct_github_url(full_path)
    except Exception as e:
        return templates.TemplateResponse(
            "github.html", 
            {
                "request": request, 
                "result": False, 
                "github_url": full_path,
                "summary": "", 
                "tree": "", 
                "content": "",
                "error_message": f"Error processing repository {e}"
            }
        )
    
    # Return the template with loading state
    return templates.TemplateResponse(
        "github.html",
        {
            "request": request,
            "result": False,  # This triggers the loading state
            "github_url": github_url,
            "summary": "",
            "tree": "",
            "content": "",
        }
    )

@app.post("/{full_path:path}", response_class=HTMLResponse)
async def process_github_path(request: Request, full_path: str, input_text: str = Form(...)):
    try:
        summary, tree, content = await process_input(input_text)
    except Exception as e:
        return templates.TemplateResponse(
            "github.html", 
            {
                "request": request, 
                "result": False, 
                "github_url": input_text,
                "summary": "", 
                "tree": "", 
                "content": "",
                "error_message": f"Error processing repository {e}"
            }
        )
    
    ingest_id = str(uuid.uuid4())
    with open(f"../tmp/ingest-{ingest_id}.txt", "w") as f:
        f.write(f"Summary:\n{summary}\n\nFile Tree:\n{tree}\n\nDetailed Content:\n{content}")
    
    if len(content) > MAX_DISPLAY_SIZE:
        content = f"(Files content cropped to {int(MAX_DISPLAY_SIZE/1000)}k characters, download full digest to see more)\n" + content[:MAX_DISPLAY_SIZE]
        
    return templates.TemplateResponse(
        "github.html", 
        {
            "request": request, 
            "summary": summary,
            "result": True, 
            "tree": tree, 
            "content": content,
            "github_url": input_text,
            "error_message": None,
            "ingest_id": ingest_id
        }
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

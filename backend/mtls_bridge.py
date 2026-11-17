"""Local-only adapter to the Windows certificate store; private key never leaves Windows."""
import hmac
import threading
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import JSONResponse
from .settings import setting,MODEL
from .transport import completion,TransportError

app=FastAPI(title='Local corporate mTLS adapter',docs_url=None,redoc_url=None,openapi_url=None)
slots=threading.BoundedSemaphore(2)

@app.get('/live')
def live():return {'status':'alive'}

@app.post('/v1/chat/completions')
async def request_completion(request:Request):
    secret=setting('LOCAL_LLM_BRIDGE_TOKEN')
    if not secret or not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+secret):raise HTTPException(401,'Unauthorized')
    if int(request.headers.get('content-length','0'))>500000:raise HTTPException(413,'Request too large')
    payload=await request.json()
    allowed=setting('LOCAL_LLM_BRIDGE_MODELS',MODEL+',deepseek-v4-flash').split(',')
    if payload.get('model') not in allowed:raise HTTPException(400,'Model is not allowed')
    if not slots.acquire(blocking=False):return JSONResponse({'error':'adapter_busy'},status_code=429,headers={'Retry-After':'3'})
    try:
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(completion,payload,150)
    except TransportError as error:return JSONResponse({'error':'corporate_gateway_unavailable'},status_code=error.status or 503,headers={'Retry-After':str(max(3,int(error.retry_after)))})
    finally:slots.release()

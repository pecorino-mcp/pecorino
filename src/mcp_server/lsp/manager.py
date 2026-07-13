import os
import sys
import json
import subprocess
import threading
import logging
import queue
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class LSPClient:
    def __init__(self, workspace_root: str, lsp_binary: str = None):
        self.workspace_root = os.path.abspath(workspace_root)
        
        # Default to pylsp in current virtualenv if not specified
        if not lsp_binary:
            venv_bin = Path(sys.executable).parent / "pylsp"
            if venv_bin.exists():
                self.lsp_binary = str(venv_bin)
            else:
                self.lsp_binary = "pylsp"
        else:
            self.lsp_binary = lsp_binary
            
        self.process: Optional[subprocess.Popen] = None
        self.read_thread: Optional[threading.Thread] = None
        self.running = False
        self._id = 0
        self._id_lock = threading.Lock()
        
        # Map request ID -> Queue to deliver response
        self.response_queues: Dict[int, queue.Queue] = {}
        self.queues_lock = threading.Lock()
        self.write_lock = threading.Lock()
        
    def start(self) -> bool:
        logger.info(f"Starting LSP server: {self.lsp_binary} in {self.workspace_root}")
        try:
            self.process = subprocess.Popen(
                [self.lsp_binary],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, # Ignore LSP stderr noise
                bufsize=0
            )
        except Exception as e:
            logger.error(f"Failed to start LSP subprocess: {e}")
            return False
            
        self.running = True
        self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.read_thread.start()
        
        # Initialize workspace
        init_res = self.send_request("initialize", {
            "processId": os.getpid(),
            "rootUri": Path(self.workspace_root).as_uri(),
            "rootPath": self.workspace_root,
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": True}
                }
            }
        }, timeout=10.0)
        
        if init_res is None:
            logger.error("LSP server failed to initialize.")
            self.stop()
            return False
            
        self.send_notification("initialized", {})
        logger.info("LSP server initialized successfully.")
        return True
        
    def stop(self):
        self.running = False
        if self.process:
            try:
                self.send_request("shutdown", {}, timeout=2.0)
                self.send_notification("exit", {})
            except Exception:
                pass
            try:
                self.process.terminate()
                self.process.wait(timeout=2.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
            
        if self.read_thread:
            self.read_thread.join(timeout=1.0)
            self.read_thread = None
            
    def _read_loop(self):
        stdout = self.process.stdout
        while self.running and stdout:
            try:
                # Read Headers
                content_length = None
                while True:
                    line = stdout.readline()
                    if not line:
                        # Stream closed
                        return
                    line = line.decode('utf-8', errors='ignore').strip()
                    if not line:
                        break # Headers section ends with blank line
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":")[1].strip())
                        
                if content_length is None:
                    continue
                    
                # Read Body
                body_bytes = stdout.read(content_length)
                if len(body_bytes) < content_length:
                    return # Stream cut short
                    
                payload = json.loads(body_bytes.decode('utf-8', errors='ignore'))
                
                # Check if it's a response
                if "id" in payload:
                    resp_id = payload["id"]
                    with self.queues_lock:
                        q = self.response_queues.get(resp_id)
                    if q:
                        q.put(payload)
            except Exception as e:
                if self.running:
                    logger.debug(f"Error in LSP read loop: {e}")
                return
                
    def send_request(self, method: str, params: dict, timeout: float = 5.0) -> Optional[dict]:
        if not self.running or not self.process or not self.process.stdin:
            return None
            
        with self._id_lock:
            self._id += 1
            req_id = self._id
            
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params
        }
        
        q = queue.Queue()
        with self.queues_lock:
            self.response_queues[req_id] = q
            
        try:
            body = json.dumps(payload)
            msg = f"Content-Length: {len(body)}\r\n\r\n{body}"
            with self.write_lock:
                self.process.stdin.write(msg.encode('utf-8'))
                self.process.stdin.flush()
            
            # Wait for response
            response = q.get(timeout=timeout)
            if "error" in response:
                logger.warning(f"LSP Error response for {method}: {response['error']}")
                return None
            return response.get("result")
        except queue.Empty:
            logger.warning(f"LSP Request {method} (id={req_id}) timed out after {timeout}s")
            return None
        except Exception as e:
            logger.error(f"LSP Send request error: {e}")
            return None
        finally:
            with self.queues_lock:
                self.response_queues.pop(req_id, None)
                
    def send_notification(self, method: str, params: dict):
        if not self.running or not self.process or not self.process.stdin:
            return
            
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        try:
            body = json.dumps(payload)
            msg = f"Content-Length: {len(body)}\r\n\r\n{body}"
            with self.write_lock:
                self.process.stdin.write(msg.encode('utf-8'))
                self.process.stdin.flush()
        except Exception as e:
            logger.error(f"LSP Send notification error: {e}")
            
    def open_document(self, filepath: str, content: str):
        self.send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": Path(filepath).as_uri(),
                "languageId": "python",
                "version": 1,
                "text": content
            }
        })
        
    def resolve_definition(self, filepath: str, line: int, character: int) -> Optional[dict]:
        """Query definition of symbol at 1-indexed line and 0-indexed character.
        
        Returns dict containing:
          - filepath: absolute path of defining file
          - start_line: 1-indexed start line
          - start_char: 0-indexed start character
        """
        res = self.send_request("textDocument/definition", {
            "textDocument": {
                "uri": Path(filepath).as_uri()
            },
            "position": {
                "line": line - 1, # LSP is 0-indexed for line
                "character": character
            }
        })
        
        if not res:
            return None
            
        # Definition can return Location or Location[]
        if isinstance(res, list):
            if not res:
                return None
            loc = res[0]
        else:
            loc = res
            
        uri = loc.get("uri")
        if not uri:
            return None
            
        # Parse URI back to path
        if uri.startswith("file://"):
            import urllib.parse
            # On windows, file:///C:/path -> C:\path, on unix, file:///path -> /path
            def_path = urllib.parse.unquote(uri[7:])
            if def_path.startswith("/") and os.name == "nt" and def_path[2] == ":":
                def_path = def_path[1:]
        else:
            return None
            
        range_val = loc.get("range", {})
        start_pos = range_val.get("start", {})
        
        return {
            "filepath": os.path.abspath(def_path),
            "start_line": start_pos.get("line", 0) + 1, # Convert back to 1-indexed
            "start_char": start_pos.get("character", 0)
        }

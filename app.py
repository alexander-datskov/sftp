import os
import tempfile
import stat as statmod
import paramiko
from flask import Flask, request, jsonify, send_file, render_template
from werkzeug.utils import secure_filename
from flask_cors import CORS
from datetime import timedelta

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=2)

# Fix CORS and session cookie issues
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True if using HTTPS

# CORS with proper credentials support
CORS(app, supports_credentials=True, resources={
    r"/*": {
        "origins": "*",
        "allow_headers": ["Content-Type"],
        "expose_headers": ["Content-Type"],
        "supports_credentials": True
    }
})

# Simple global client (reverted from session-based for simplicity)
# The session-based approach was causing the connection issues
sftp_client = None

# SFTP client wrapper
class SFTPClient:
    def __init__(self, host, port, username, password):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.client = None
        self.sftp = None

    def connect(self):
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # Add more connection parameters for better compatibility
            self.client.connect(
                self.host, 
                port=self.port, 
                username=self.username, 
                password=self.password, 
                timeout=30,
                banner_timeout=30,
                auth_timeout=30,
                look_for_keys=False,  # Don't look for SSH keys
                allow_agent=False     # Don't use SSH agent
            )
            self.sftp = self.client.open_sftp()
            return True
        except paramiko.AuthenticationException as e:
            return f"Error: Authentication failed - {str(e)}"
        except paramiko.SSHException as e:
            return f"Error: SSH connection failed - {str(e)}"
        except TimeoutError as e:
            return f"Error: Connection timeout - {str(e)}"
        except Exception as e:
            return f"Error: {type(e).__name__} - {str(e)}"

    def _join_path(self, *parts):
        """Join paths using forward slashes for SFTP compatibility"""
        path = '/'.join(str(p) for p in parts)
        while '//' in path:
            path = path.replace('//', '/')
        return path

    def listdir(self, path='/'):
        try:
            items = []
            for attr in self.sftp.listdir_attr(path):
                item_path = self._join_path(path, attr.filename)
                is_dir = statmod.S_ISDIR(attr.st_mode)
                items.append({
                    'name': attr.filename,
                    'path': item_path,
                    'is_dir': bool(is_dir),
                    'size': attr.st_size if hasattr(attr, 'st_size') else 0,
                    'mtime': attr.st_mtime if hasattr(attr, 'st_mtime') else 0
                })
            items.sort(key=lambda i: (not i['is_dir'], i['name'].lower()))
            return items
        except FileNotFoundError:
            return f"Error: Directory not found: {path}"
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error: {str(e)}"

    def read_file(self, path, max_bytes=None):
        try:
            with self.sftp.open(path, 'rb') as f:
                data = f.read() if max_bytes is None else f.read(max_bytes)
                return data.decode('utf-8', errors='replace')
        except FileNotFoundError:
            return f"Error: File not found: {path}"
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error: {str(e)}"

    def write_file(self, path, content):
        try:
            with self.sftp.open(path, 'w') as f:
                if isinstance(content, str):
                    f.write(content)
                else:
                    f.write(content.decode('utf-8'))
            return True
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error: {str(e)}"

    def mkdir(self, path):
        try:
            self.sftp.mkdir(path)
            return True
        except FileExistsError:
            return f"Error: Directory already exists: {path}"
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error: {str(e)}"

    def remove(self, path):
        try:
            try:
                st = self.sftp.stat(path)
            except IOError:
                return f"Error: Path not found: {path}"
            
            if statmod.S_ISDIR(st.st_mode):
                self.sftp.rmdir(path)
            else:
                self.sftp.remove(path)
            return True
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except OSError as e:
            if "not empty" in str(e).lower():
                return f"Error: Directory not empty: {path}"
            return f"Error: {str(e)}"
        except Exception as e:
            return f"Error: {str(e)}"

    def rename(self, old, new):
        try:
            self.sftp.rename(old, new)
            return True
        except FileNotFoundError:
            return f"Error: File not found: {old}"
        except PermissionError:
            return f"Error: Permission denied"
        except Exception as e:
            return f"Error: {str(e)}"

    def upload_local(self, local_path, remote_path):
        try:
            self.sftp.put(local_path, remote_path)
            return True
        except PermissionError:
            return f"Error: Permission denied: {remote_path}"
        except Exception as e:
            return f"Error: {str(e)}"

    def download_to_local(self, remote_path, local_path):
        try:
            st = self.sftp.stat(remote_path)
            if statmod.S_ISDIR(st.st_mode):
                return f"Error: '{remote_path}' is a directory"
            self.sftp.get(remote_path, local_path)
            return True
        except FileNotFoundError:
            return f"Error: File not found: {remote_path}"
        except PermissionError:
            return f"Error: Permission denied: {remote_path}"
        except Exception as e:
            return f"Error: {str(e)}"

    def stat(self, path):
        try:
            st = self.sftp.stat(path)
            return {
                'size': st.st_size,
                'mtime': st.st_mtime,
                'mode': st.st_mode,
                'is_dir': bool(statmod.S_ISDIR(st.st_mode))
            }
        except FileNotFoundError:
            return f"Error: Path not found: {path}"
        except Exception as e:
            return f"Error: {str(e)}"

    def close(self):
        try:
            if self.sftp: 
                self.sftp.close()
            if self.client: 
                self.client.close()
        except Exception:
            pass

# Serve frontend
@app.route('/')
def index():
    return render_template('index.html')

# Connect
@app.route('/connect', methods=['POST'])
def connect_route():
    global sftp_client
    
    data = request.json or {}
    host = data.get('host')
    port = data.get('port', 22)
    username = data.get('username')
    password = data.get('password')

    if not host or not username or password is None:
        return jsonify(success=False, message="host, username and password required"), 400

    # Close existing connection if any
    if sftp_client:
        try:
            sftp_client.close()
        except:
            pass
    
    sftp_client = SFTPClient(host, port, username, password)
    res = sftp_client.connect()
    
    if res is True:
        return jsonify(success=True, message="Connected successfully")
    else:
        sftp_client = None
        return jsonify(success=False, message=res), 500

# List directory contents
@app.route('/api/list', methods=['GET'])
def api_list():
    global sftp_client
    if sftp_client is None:
        return jsonify(success=False, message="Not connected"), 400
    
    path = request.args.get('path', '/')
    result = sftp_client.listdir(path)
    
    if isinstance(result, list):
        return jsonify(success=True, items=result)
    else:
        return jsonify(success=False, message=result), 500

# Read file content
@app.route('/api/read', methods=['GET'])
def api_read():
    global sftp_client
    if sftp_client is None:
        return jsonify(success=False, message="Not connected"), 400
    
    path = request.args.get('path')
    if not path:
        return jsonify(success=False, message="Missing path"), 400
    
    max_bytes = request.args.get('max', None)
    max_bytes = int(max_bytes) if max_bytes else None
    result = sftp_client.read_file(path, max_bytes)
    
    if isinstance(result, str) and not result.startswith("Error:"):
        return jsonify(success=True, content=result)
    else:
        return jsonify(success=False, message=result), 500

# Write file content
@app.route('/api/write', methods=['POST'])
def api_write():
    global sftp_client
    if sftp_client is None:
        return jsonify(success=False, message="Not connected"), 400
    
    data = request.json or {}
    path = data.get('path')
    content = data.get('content', '')
    
    if not path:
        return jsonify(success=False, message="Missing path"), 400
    
    result = sftp_client.write_file(path, content)
    
    if result is True:
        return jsonify(success=True)
    else:
        return jsonify(success=False, message=result), 500

# Make directory
@app.route('/api/mkdir', methods=['POST'])
def api_mkdir():
    global sftp_client
    if sftp_client is None:
        return jsonify(success=False, message="Not connected"), 400
    
    data = request.json or {}
    path = data.get('path')
    
    if not path:
        return jsonify(success=False, message="Missing path"), 400
    
    res = sftp_client.mkdir(path)
    
    if res is True:
        return jsonify(success=True)
    else:
        return jsonify(success=False, message=res), 500

# Remove file or directory
@app.route('/api/remove', methods=['POST'])
def api_remove():
    global sftp_client
    if sftp_client is None:
        return jsonify(success=False, message="Not connected"), 400
    
    data = request.json or {}
    path = data.get('path')
    
    if not path:
        return jsonify(success=False, message="Missing path"), 400
    
    res = sftp_client.remove(path)
    
    if res is True:
        return jsonify(success=True)
    else:
        return jsonify(success=False, message=res), 500

# Rename/move
@app.route('/api/rename', methods=['POST'])
def api_rename():
    global sftp_client
    if sftp_client is None:
        return jsonify(success=False, message="Not connected"), 400
    
    data = request.json or {}
    old = data.get('old')
    new = data.get('new')
    
    if not old or not new:
        return jsonify(success=False, message="Missing old or new path"), 400
    
    res = sftp_client.rename(old, new)
    
    if res is True:
        return jsonify(success=True)
    else:
        return jsonify(success=False, message=res), 500

# Upload file
@app.route('/api/upload', methods=['POST'])
def api_upload():
    global sftp_client
    if sftp_client is None:
        return jsonify(success=False, message="Not connected"), 400
    
    if 'file' not in request.files:
        return jsonify(success=False, message="No file provided"), 400
    
    f = request.files['file']
    if f.filename == '':
        return jsonify(success=False, message="No file selected"), 400
    
    remote_dir = request.form.get('remote_dir', '/')
    filename = secure_filename(f.filename)
    remote_path = f"{remote_dir.rstrip('/')}/{filename}"
    
    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        f.save(tmp.name)
        tmp.close()
        res = sftp_client.upload_local(tmp.name, remote_path)
        
        if res is True:
            return jsonify(success=True)
        else:
            return jsonify(success=False, message=res), 500
    finally:
        try:
            os.remove(tmp.name)
        except Exception:
            pass

# Download file
@app.route('/api/download', methods=['GET'])
def api_download():
    global sftp_client
    if sftp_client is None:
        return jsonify(success=False, message="Not connected"), 400
    
    path = request.args.get('path')
    if not path:
        return jsonify(success=False, message="Missing path"), 400

    tmp_fd, tmp_path = tempfile.mkstemp()
    os.close(tmp_fd)
    res = sftp_client.download_to_local(path, tmp_path)
    
    if res is True:
        try:
            filename = os.path.basename(path)
            return send_file(
                tmp_path, 
                as_attachment=True, 
                download_name=filename,
                mimetype='application/octet-stream'
            )
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    else:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return jsonify(success=False, message=res), 500

# Stat (info)
@app.route('/api/stat', methods=['GET'])
def api_stat():
    global sftp_client
    if sftp_client is None:
        return jsonify(success=False, message="Not connected"), 400
    
    path = request.args.get('path')
    if not path:
        return jsonify(success=False, message="Missing path"), 400
    
    res = sftp_client.stat(path)
    
    if isinstance(res, dict):
        return jsonify(success=True, info=res)
    else:
        return jsonify(success=False, message=res), 500

# Disconnect
@app.route('/disconnect', methods=['POST'])
def disconnect_route():
    global sftp_client
    if sftp_client:
        try:
            sftp_client.close()
        except:
            pass
        sftp_client = None
    return jsonify(success=True)

if __name__ == '__main__':
    # Use environment variable for debug mode
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=1867, debug=debug_mode, threaded=True)

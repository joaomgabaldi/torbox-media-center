from library.app import RAW_MODE
import os
from library.filesystem import MOUNT_PATH
import stat
import errno
from functions.torboxFunctions import getDownloadLink
from library.http import general_http_client
import time
import sys
import logging
from functions.appFunctions import getAllUserDownloads
import threading
import httpx
import random
from sys import platform

try:
    import _find_fuse_parts # type: ignore # noqa: F401
except ImportError:
    pass
import fuse
from fuse import Fuse
if not hasattr(fuse, '__version__'):
    raise RuntimeError("your fuse-python doesn't know of fuse.__version__, probably it's too old.")

fuse.fuse_python_api = (0, 2)

LINK_AGE = 3 * 60 * 60 # 3 hours

class VirtualFileSystem:
    def __init__(self, files_list):
        self.files = files_list
        self.structure = { '/': set() }
        self.file_map = {}
        self._build()

    def _build(self):
        for f in self.files:
            original_path = f.get("path")
            if not original_path:
                continue
            
            original_path = original_path.lstrip('/')
            parts = original_path.split('/')
            
            file_path = f"/{original_path}"
            self.file_map[file_path] = f
            
            current_path = ""
            for part in parts[:-1]:
                parent = current_path if current_path else "/"
                current_path = f"{current_path}/{part}" if current_path else f"/{part}"
                
                self.structure.setdefault(parent, set()).add(part)
                self.structure.setdefault(current_path, set())
            
            file_name = parts[-1]
            parent = current_path if current_path else "/"
            self.structure.setdefault(parent, set()).add(file_name)

        for key in self.structure:
            self.structure[key] = sorted(list(self.structure[key]))

    def is_dir(self, path):
        return path in self.structure
        
    def is_file(self, path):
        return path in self.file_map
        
    def get_file(self, path):
        return self.file_map.get(path)
        
    def list_dir(self, path):
        return self.structure.get(path, [])

class TorboxStream:
    """Mantém um fluxo HTTP persistente para leituras sequenciais do SO, bypassando rate limits."""
    def __init__(self, url, offset, file_size):
        self.url = url
        self.offset = offset
        self.file_size = file_size
        self.buffer = bytearray()
        self.cursor = offset
        self.closed = False
        
        headers = {
            "Range": f"bytes={offset}-",
            **general_http_client.headers
        }
        
        request = general_http_client.build_request("GET", url, headers=headers)
        
        for attempt in range(5):
            try:
                self.response = general_http_client.send(request, stream=True)
                if self.response.status_code in [429]:
                    self.response.close()
                    time.sleep(1.5 * (2 ** attempt) + random.uniform(0.1, 0.5))
                    continue
                self.response.raise_for_status()
                break
            except httpx.RequestError as e:
                time.sleep(1.5 * (2 ** attempt) + random.uniform(0.1, 0.5))
                if attempt == 4: raise
        
        # O iter_bytes atua como pull; só traciona dados da rede quando bufferizado ativamente
        self.iterator = self.response.iter_bytes(chunk_size=1 * 1024 * 1024)

    def read(self, size):
        if self.closed:
            return b''
            
        while len(self.buffer) < size:
            try:
                chunk = next(self.iterator)
                self.buffer.extend(chunk)
            except StopIteration:
                break
            except Exception as e:
                logging.error(f"Stream read error: {e}")
                break
        
        read_size = min(size, len(self.buffer))
        data = bytes(self.buffer[:read_size])
        self.buffer = self.buffer[read_size:]
        self.cursor += read_size
        return data
        
    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.response.close()
            except:
                pass

class FuseStat(fuse.Stat):
    def __init__(self):
        self.st_mode = 0
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = 0
        self.st_atime = 0
        self.st_mtime = 0
        self.st_ctime = 0

class TorBoxMediaCenterFuse(Fuse):
    def __init__(self, *args, **kwargs):
        super(TorBoxMediaCenterFuse, self).__init__(*args, **kwargs)

        threading.Thread(target=self.getFiles, daemon=True).start()

        self.files = []
        self.vfs = VirtualFileSystem(self.files)
        self.cached_links = {}
        
        self.active_streams = {}
        self.stream_locks = {}
        self.global_state_lock = threading.Lock()
        self.MAX_CONCURRENT_STREAMS = 12

    def getFiles(self):
        while True:
            files = getAllUserDownloads()
            if files:
                self.files = files
                self.vfs = VirtualFileSystem(self.files)
                logging.debug(f"Updated {len(self.files)} files in VFS")
            time.sleep(300)

    def _get_stream_lock(self, path):
        with self.global_state_lock:
            if path not in self.stream_locks:
                self.stream_locks[path] = threading.Lock()
            return self.stream_locks[path]
        
    def getattr(self, path):
        st = FuseStat()
        now = int(time.time())
        st.st_atime = now
        st.st_mtime = now
        st.st_ctime = now
        
        st.st_uid = os.getuid()
        st.st_gid = os.getgid()
        
        if self.vfs.is_dir(path):
            st.st_mode = stat.S_IFDIR | 0o755
            st.st_nlink = 2
            return st
        elif self.vfs.is_file(path):
            file_info = self.vfs.get_file(path)
            if not file_info:
                return -errno.ENOENT
            st.st_mode = stat.S_IFREG | 0o444
            st.st_nlink = 1
            st.st_size = file_info.get('file_size', 0)
            return st
            
        return -errno.ENOENT
    
    def readdir(self, path, _):
        if not self.vfs.is_dir(path):
            return -errno.ENOENT
            
        yield fuse.Direntry('.')
        yield fuse.Direntry('..')
        
        for item in self.vfs.list_dir(path):
            yield fuse.Direntry(item)
    
    def open(self, _, flags):
        accmode = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
        if (flags & accmode) != os.O_RDONLY:
            return -errno.EACCES
    
    def read(self, path, size, offset):
        logging.debug(f"READ Path: {path} Size: {size} Offset: {offset}")
        file = self.vfs.get_file(path)

        if not file:
            return -errno.ENOENT
        
        file_size = file.get('file_size', 0)
        
        if offset >= file_size:
            return b''
            
        if offset + size > file_size:
            size = file_size - offset
            
        # Resolução de link via API 
        current_time = time.time()
        with self.global_state_lock:
            needs_link = path not in self.cached_links or current_time - self.cached_links[path]['timestamp'] > LINK_AGE
            
        if needs_link:
            logging.debug(f"Resolving fresh API link for {path}")
            link = getDownloadLink(file.get('download_link'))
            with self.global_state_lock:
                self.cached_links[path] = {
                    'link': link,
                    'timestamp': time.time()
                }

        with self.global_state_lock:
            download_link = self.cached_links[path]['link']
            
        lock = self._get_stream_lock(path)
        
        with lock:
            stream = self.active_streams.get(path)
            
            if stream:
                if stream.cursor == offset:
                    pass
                elif offset > stream.cursor and offset - stream.cursor <= 4 * 1024 * 1024:
                    # Se o SO saltou uma porção pequena de bytes, ignora fechamento e puxa bytes passivos 
                    stream.read(offset - stream.cursor)
                else:
                    # Seek intencional acionado: mata a conexão antiga e destrói o buffer
                    stream.close()
                    stream = None
                    
            if not stream:
                with self.global_state_lock:
                    if len(self.active_streams) >= self.MAX_CONCURRENT_STREAMS:
                        # Expurga soquetes de arquivos passados caso o SO abra muitos arquivos concorrentes
                        paths_to_kill = [p for p in list(self.active_streams.keys()) if p != path]
                        if paths_to_kill:
                            kill_path = paths_to_kill[0]
                            self.active_streams[kill_path].close()
                            del self.active_streams[kill_path]
                            
                stream = TorboxStream(download_link, offset, file_size)
                self.active_streams[path] = stream
                
            return stream.read(size)
    
    def release(self, path, fh):
        # Ação crítica: Quando o File Explorer termina de extrair metadados, o soquete é desconectado instantaneamente.
        with self.global_state_lock:
            if path in self.active_streams:
                self.active_streams[path].close()
                del self.active_streams[path]
        return 0
    
def runFuse():
    server = TorBoxMediaCenterFuse(
        version="%prog " + fuse.__version__,
        usage="%prog [options] mountpoint",
        dash_s_do="setsingle",
    )

    server.parser.add_option(
        mountopt="root",
        metavar="PATH",
        default=MOUNT_PATH,
        help="Mount point for the filesystem",
    )
    if platform != "darwin":
        server.fuse_args.add(
            "nonempty"
        )
    server.fuse_args.add(
        "allow_other"
    )
    server.fuse_args.add(
        "-f"
    )
    server.parse(values=server, errex=1)
    try:
        server.fuse_args.mountpoint = MOUNT_PATH
    except OSError as e:
        logging.error(f"Error changing directory: {e}")
        sys.exit(1)
    server.main()

def unmountFuse():
    try:
        os.system("fusermount -u " + MOUNT_PATH)
    except OSError as e:
        logging.error(f"Error unmounting: {e}")
        sys.exit(1)
    logging.info("Unmounted successfully.")
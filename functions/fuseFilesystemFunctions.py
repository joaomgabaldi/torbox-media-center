from library.app import RAW_MODE
import os
from library.filesystem import MOUNT_PATH
import stat
import errno
from functions.torboxFunctions import getDownloadLink, downloadFile
import time
import sys
import logging
from functions.appFunctions import getAllUserDownloads
import threading
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
            
            # Remove barras iniciais para evitar elementos vazios no split
            original_path = original_path.lstrip('/')
            parts = original_path.split('/')
            
            # Mapeamento do arquivo completo
            file_path = f"/{original_path}"
            self.file_map[file_path] = f
            
            # Construção recursiva da estrutura de diretórios
            current_path = ""
            for part in parts[:-1]:
                parent = current_path if current_path else "/"
                current_path = f"{current_path}/{part}" if current_path else f"/{part}"
                
                self.structure.setdefault(parent, set()).add(part)
                self.structure.setdefault(current_path, set())
            
            # Adiciona o arquivo no diretório correspondente
            file_name = parts[-1]
            parent = current_path if current_path else "/"
            self.structure.setdefault(parent, set()).add(file_name)

        # Ordenação consistente
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
        self.file_handles = {}
        self.next_handle = 1
        self.cached_links = {}
        self.read_buffers = {}
        self.CHUNK_SIZE = 4 * 1024 * 1024 # 4MB

    def getFiles(self):
        while True:
            files = getAllUserDownloads()
            if files:
                self.files = files
                self.vfs = VirtualFileSystem(self.files)
                logging.debug(f"Updated {len(self.files)} files in VFS")
            time.sleep(300)
        
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
        
        current_time = time.time()
        if path not in self.cached_links:
            self.cached_links[path] = {
                'link': getDownloadLink(file.get('download_link')),
                'timestamp': current_time
            }
        elif current_time - self.cached_links[path]['timestamp'] > LINK_AGE:
            download_link = getDownloadLink(file.get('download_link'))
            self.cached_links[path] = {
                'link': download_link,
                'timestamp': current_time
            }
        download_link = self.cached_links[path]['link']
        
        buffer_info = self.read_buffers.get(path)
        if buffer_info:
            buf_offset, buf_data = buffer_info
            # Verifica se os bytes requisitados estão inteiramente no micro-buffer atual
            if buf_offset <= offset and (offset + size) <= (buf_offset + len(buf_data)):
                start_idx = offset - buf_offset
                return buf_data[start_idx : start_idx + size]

        # Cache miss. Faz o download do bloco alocado predefinido ou do tamanho da requisição, se maior
        fetch_size = max(size, self.CHUNK_SIZE)
        if offset + fetch_size > file_size:
            fetch_size = file_size - offset
            
        try:
            block_data = downloadFile(download_link, fetch_size, offset)
            if not block_data:
                return -errno.EIO
            
            # Sobrescreve blocos velhos daquele caminho imediatamente
            self.read_buffers[path] = (offset, block_data)
            return block_data[:size]
        except Exception as e:
            logging.error(f"Error reading file: {e}")
            return -errno.EIO
    
    def release(self, path, fh):
        if fh in self.file_handles:
            del self.file_handles[fh]
        if path in self.read_buffers:
            del self.read_buffers[path]
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

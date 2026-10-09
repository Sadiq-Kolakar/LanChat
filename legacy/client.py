import socket, threading, json, queue, tkinter as tk
from tkinter import ttk, messagebox, simpledialog

PORT = 5050

class LanChat:
    def __init__(self, root):
        self.root = root
        self.root.title('LAN Chat')
        self.root.geometry('820x540')
        self.root.minsize(700, 450)
        self.sock = None
        self.reader_thread = None
        self.events = queue.Queue()
        self.username = ''
        self.server_ip = ''
        self.users = []
        self.build_ui()
        self.root.after(100, self.process_events)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.ask_connection()

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill='x')
        ttk.Label(top, text='LAN CHAT', font=('Segoe UI', 18, 'bold')).pack(side='left')
        self.status = ttk.Label(top, text='Disconnected')
        self.status.pack(side='left', padx=18)
        ttk.Button(top, text='Change username', command=self.change_username).pack(side='right')

        body = ttk.Frame(self.root, padding=(10,0,10,10))
        body.pack(fill='both', expand=True)
        left = ttk.LabelFrame(body, text='Online users', padding=8)
        left.pack(side='left', fill='y')
        self.user_listbox = tk.Listbox(left, width=22, borderwidth=0, highlightthickness=0)
        self.user_listbox.pack(fill='both', expand=True)

        right = ttk.Frame(body)
        right.pack(side='left', fill='both', expand=True, padx=(10,0))
        self.chat = tk.Text(right, wrap='word', state='disabled', font=('Segoe UI', 11), padx=10, pady=10)
        self.chat.pack(fill='both', expand=True)

        bottom = ttk.Frame(right, padding=(0,8,0,0))
        bottom.pack(fill='x')
        self.entry = ttk.Entry(bottom, font=('Segoe UI', 11))
        self.entry.pack(side='left', fill='x', expand=True)
        self.entry.bind('<Return>', lambda e: self.send_message())
        ttk.Button(bottom, text='Send', command=self.send_message).pack(side='left', padx=(8,0))

    def ask_connection(self):
        self.server_ip = simpledialog.askstring('Connect to LAN Chat', 'Server PC IP address:', initialvalue='127.0.0.1', parent=self.root)
        if not self.server_ip:
            self.root.after(100, self.close)
            return
        name = simpledialog.askstring('Username', 'Choose your username:', initialvalue='Guest', parent=self.root)
        if not name:
            name = 'Guest'
        self.username = name.strip()[:24] or 'Guest'
        self.connect()

    def connect(self):
        try:
            self.sock = socket.create_connection((self.server_ip, PORT), timeout=5)
            self.sock.settimeout(None)
            self.send({'type':'join','username':self.username})
            self.status.config(text=f'Connected to {self.server_ip}')
            self.reader_thread = threading.Thread(target=self.read_loop, daemon=True)
            self.reader_thread.start()
        except OSError as e:
            messagebox.showerror('Connection failed', f'Could not connect to {self.server_ip}:{PORT}\n\n{e}')
            self.status.config(text='Disconnected')

    def send(self, obj):
        self.sock.sendall((json.dumps(obj)+'\n').encode('utf-8'))

    def read_loop(self):
        buf = ''
        try:
            while True:
                data = self.sock.recv(4096)
                if not data: break
                buf += data.decode('utf-8')
                while '\n' in buf:
                    line, buf = buf.split('\n',1)
                    if line.strip(): self.events.put(json.loads(line))
        except OSError:
            pass
        self.events.put({'type':'disconnected'})

    def process_events(self):
        try:
            while True:
                msg = self.events.get_nowait()
                typ = msg.get('type')
                if typ == 'welcome':
                    self.username = msg.get('username', self.username)
                    self.users = msg.get('users', [])
                    self.refresh_users()
                    self.add_line(f'Connected as {self.username}')
                elif typ == 'users':
                    self.users = msg.get('users', [])
                    self.refresh_users()
                elif typ == 'chat':
                    self.add_line(f"{msg.get('username','?')}: {msg.get('text','')}")
                elif typ == 'system':
                    self.add_line(f"• {msg.get('text','')}")
                elif typ == 'disconnected':
                    self.status.config(text='Disconnected')
                    self.add_line('• Connection closed.')
        except queue.Empty:
            pass
        self.root.after(100, self.process_events)

    def refresh_users(self):
        self.user_listbox.delete(0, tk.END)
        for user in self.users:
            self.user_listbox.insert(tk.END, '🟢 ' + user)

    def add_line(self, text):
        self.chat.config(state='normal')
        self.chat.insert(tk.END, text + '\n')
        self.chat.see(tk.END)
        self.chat.config(state='disabled')

    def send_message(self):
        text = self.entry.get().strip()
        if not text or not self.sock: return
        try:
            self.send({'type':'chat','text':text})
            self.entry.delete(0, tk.END)
        except OSError:
            messagebox.showerror('Error','Connection was lost.')

    def change_username(self):
        new = simpledialog.askstring('Username', 'Username is fixed for this session in the prototype.\nRestart to choose another username.', initialvalue=self.username, parent=self.root)

    def close(self):
        try:
            if self.sock: self.sock.close()
        except OSError: pass
        self.root.destroy()

if __name__ == '__main__':
    root = tk.Tk()
    LanChat(root)
    root.mainloop()

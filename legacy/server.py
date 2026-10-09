import socket, threading, json, sys

HOST = '0.0.0.0'
PORT = 5050
clients = {}  # socket -> username
lock = threading.Lock()

def send(sock, obj):
    data = (json.dumps(obj) + '\n').encode('utf-8')
    sock.sendall(data)

def broadcast(obj, exclude=None):
    dead = []
    with lock:
        sockets = list(clients)
    for s in sockets:
        if s is exclude:
            continue
        try:
            send(s, obj)
        except OSError:
            dead.append(s)
    for s in dead:
        remove_client(s)

def user_list():
    with lock:
        return sorted(clients.values())

def remove_client(sock):
    with lock:
        name = clients.pop(sock, None)
    try:
        sock.close()
    except OSError:
        pass
    if name:
        print(f'- {name} disconnected')
        broadcast({'type':'system','text':f'{name} left the chat'})
        broadcast({'type':'users','users':user_list()})

def handle_client(sock, addr):
    buffer = ''
    name = None
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            buffer += data.decode('utf-8')
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                if not line.strip():
                    continue
                msg = json.loads(line)
                if msg.get('type') == 'join' and name is None:
                    proposed = str(msg.get('username','Guest')).strip()[:24] or 'Guest'
                    with lock:
                        existing = set(clients.values())
                        name = proposed
                        n = 2
                        while name in existing:
                            name = f'{proposed[:20]}_{n}'
                            n += 1
                        clients[sock] = name
                    print(f'+ {name} connected from {addr[0]}')
                    send(sock, {'type':'welcome','username':name,'users':user_list()})
                    broadcast({'type':'system','text':f'{name} joined the chat'}, exclude=sock)
                    broadcast({'type':'users','users':user_list()})
                elif msg.get('type') == 'chat' and name:
                    text = str(msg.get('text','')).strip()
                    if text:
                        text = text[:2000]
                        broadcast({'type':'chat','username':name,'text':text})
    except (OSError, json.JSONDecodeError):
        pass
    finally:
        remove_client(sock)

def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(20)
    print(f'LAN Chat server listening on 0.0.0.0:{PORT}')
    print('Press Ctrl+C to stop.')
    try:
        while True:
            sock, addr = server.accept()
            threading.Thread(target=handle_client, args=(sock, addr), daemon=True).start()
    except KeyboardInterrupt:
        print('\nStopping server...')
    finally:
        server.close()

if __name__ == '__main__':
    main()

# reverse-proxy

This Python reverse proxy server implements multiple load balancing algorithms while handling various HTTP methods. Here's what it provides:

### Load Balancing Algorithms

1. **Round Robin**: Cycles through servers sequentially
2. **Random**: Selects servers randomly
3. **Least Connections**: Chooses the server with the fewest active connections
4. **Weighted Round Robin**: Distributes load based on server weights
5. **IP Hash**: Maps client IPs to specific servers (useful for session persistence)
6. **Least Response Time**: Selects servers with the lowest average response time
7. **Health Check**: Only routes to servers that pass health checks

### Key Features

- Multithreaded request handling
- HTTP/1.1 support for various methods (GET, POST, PUT, DELETE, HEAD)
- Proper header forwarding including X-Forwarded-* headers
- Connection pooling and error handling
- Response time monitoring
- Health checking with automatic server removal/addition

### Usage

```bash
python3 reverse_proxy.py --port 8080 --backends "localhost:8081,localhost:8082,localhost:8083" --algorithm least_connections
```

To test different algorithms, you can specify any of the implemented ones:

```bash
python3 reverse_proxy.py --algorithm round_robin --backends "localhost:8081,localhost:8082"
python3 reverse_proxy.py --algorithm least_response_time --backends "localhost:8081,localhost:8082"
```


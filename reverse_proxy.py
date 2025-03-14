#!/usr/bin/env python3
import argparse
import http.server
import http.client
import logging
import random
import socket
import sys
import threading
import time
import queue
import urllib.parse
from collections import defaultdict, Counter

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('reverse_proxy')

class LoadBalancer:
    """Base class for load balancing algorithms"""
    def __init__(self, servers):
        self.servers = servers
        self.initialize()

    def initialize(self):
        """Initialize any needed data structures"""
        pass

    def get_server(self):
        """Return the next server to use"""
        raise NotImplementedError("Subclasses must implement get_server()")

    def mark_success(self, server):
        """Mark a successful request to a server"""
        pass

    def mark_failure(self, server):
        """Mark a failed request to a server"""
        pass


class RoundRobinBalancer(LoadBalancer):
    """Simple round-robin load balancing"""
    def initialize(self):
        self.index = 0
        self.lock = threading.Lock()

    def get_server(self):
        with self.lock:
            server = self.servers[self.index]
            self.index = (self.index + 1) % len(self.servers)
            return server


class RandomBalancer(LoadBalancer):
    """Random server selection"""
    def get_server(self):
        return random.choice(self.servers)


class LeastConnectionsBalancer(LoadBalancer):
    """Selects the server with the fewest active connections"""
    def initialize(self):
        self.connections = {server: 0 for server in self.servers}
        self.lock = threading.Lock()

    def get_server(self):
        with self.lock:
            server = min(self.connections.items(), key=lambda x: x[1])[0]
            self.connections[server] += 1
            return server

    def mark_success(self, server):
        with self.lock:
            self.connections[server] = max(0, self.connections[server] - 1)

    def mark_failure(self, server):
        with self.lock:
            self.connections[server] = max(0, self.connections[server] - 1)


class WeightedRoundRobinBalancer(LoadBalancer):
    """Round-robin selection with server weights"""
    def initialize(self):
        # For simplicity, we'll assign weights based on server index + 1
        self.weights = {server: i + 1 for i, server in enumerate(self.servers)}
        self.current_weights = {server: 0 for server in self.servers}
        self.lock = threading.Lock()

    def get_server(self):
        with self.lock:
            total_weight = sum(self.weights.values())
            max_weight = -1
            selected_server = None

            for server in self.servers:
                self.current_weights[server] += self.weights[server]
                if self.current_weights[server] > max_weight:
                    max_weight = self.current_weights[server]
                    selected_server = server

            self.current_weights[selected_server] -= total_weight
            return selected_server


class IPHashBalancer(LoadBalancer):
    """Hash client IP to determine server"""
    def get_server(self, client_ip=None):
        if client_ip:
            # Use the last octet of the IP for simplicity
            try:
                hash_value = int(client_ip.split('.')[-1])
                return self.servers[hash_value % len(self.servers)]
            except (ValueError, IndexError):
                pass
        # Fallback to random selection
        return random.choice(self.servers)


class LeastResponseTimeBalancer(LoadBalancer):
    """Selects the server with the lowest average response time"""
    def initialize(self):
        self.response_times = {server: 1.0 for server in self.servers}  # Default to 1ms
        self.lock = threading.Lock()

    def get_server(self):
        with self.lock:
            return min(self.response_times.items(), key=lambda x: x[1])[0]

    def update_response_time(self, server, response_time):
        """Update the average response time for a server"""
        with self.lock:
            # Simple exponential moving average with alpha=0.3
            alpha = 0.3
            self.response_times[server] = (alpha * response_time + 
                                          (1 - alpha) * self.response_times[server])


class HealthCheckBalancer(LoadBalancer):
    """Selects servers that pass health checks"""
    def initialize(self):
        self.healthy_servers = set(self.servers)
        self.lock = threading.Lock()
        
        # Start health check thread
        self.health_check_thread = threading.Thread(
            target=self._health_check_worker,
            daemon=True
        )
        self.health_check_thread.start()

    def _health_check_worker(self):
        """Background thread that periodically checks server health"""
        while True:
            for server in self.servers:
                host, port = server.split(':')
                try:
                    conn = http.client.HTTPConnection(host, int(port), timeout=2)
                    conn.request("HEAD", "/health")
                    response = conn.getresponse()
                    
                    with self.lock:
                        if 200 <= response.status < 400:
                            self.healthy_servers.add(server)
                        else:
                            self.healthy_servers.discard(server)
                except Exception:
                    with self.lock:
                        self.healthy_servers.discard(server)
                finally:
                    conn.close()
                    
            time.sleep(10)  # Check every 10 seconds

    def get_server(self):
        with self.lock:
            if not self.healthy_servers:
                # If no healthy servers, try any server
                return random.choice(self.servers)
            return random.choice(list(self.healthy_servers))


class ReverseProxyHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.balancer = None
        self.response_time_balancer = None
        self.backends = []
        super().__init__(*args, **kwargs)
        
    def setup(self):
        self.balancer = self.server.balancer
        self.response_time_balancer = self.server.response_time_balancer
        self.backends = self.server.backends
        super().setup()

    def log_message(self, format, *args):
        logger.info(format % args)

    def do_HEAD(self):
        self.forward_request("HEAD")

    def do_GET(self):
        self.forward_request("GET")

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else None
        self.forward_request("POST", post_data)

    def do_PUT(self):
        content_length = int(self.headers.get('Content-Length', 0))
        put_data = self.rfile.read(content_length) if content_length > 0 else None
        self.forward_request("PUT", put_data)

    def do_DELETE(self):
        self.forward_request("DELETE")

    def forward_request(self, method, body=None):
        client_ip = self.client_address[0]
        
        # Special case for IP hash balancer
        if isinstance(self.balancer, IPHashBalancer):
            backend = self.balancer.get_server(client_ip)
        else:
            backend = self.balancer.get_server()
            
        host, port = backend.split(':')
        
        logger.info(f"Forwarding {method} {self.path} to {backend}")
        
        try:
            # Record start time for response time tracking
            start_time = time.time()
            
            # Create connection to backend server
            conn = http.client.HTTPConnection(host, int(port), timeout=10)
            
            # Prepare headers
            headers = {k: v for k, v in self.headers.items()}
            headers['X-Forwarded-For'] = client_ip
            headers['X-Forwarded-Host'] = self.headers.get('Host', '')
            headers['X-Forwarded-Proto'] = 'http'
            
            # Send request
            conn.request(method, self.path, body, headers)
            
            # Get response
            response = conn.getresponse()
            response_data = response.read()
            
            # Record response time
            response_time = time.time() - start_time
            if self.response_time_balancer:
                self.response_time_balancer.update_response_time(backend, response_time)
            
            # Send response to client
            self.send_response(response.status, response.reason)
            
            # Forward response headers
            for header, value in response.getheaders():
                if header.lower() not in ('transfer-encoding', 'connection'):
                    self.send_header(header, value)
            self.end_headers()
            
            # Send response body
            self.wfile.write(response_data)
            
            # Mark as successful connection
            self.balancer.mark_success(backend)
            
        except Exception as e:
            logger.error(f"Error forwarding to {backend}: {e}")
            self.balancer.mark_failure(backend)
            
            try:
                self.send_error(502, f"Bad Gateway: {str(e)}")
            except:
                pass
        finally:
            if 'conn' in locals():
                conn.close()


class ReverseProxyServer(http.server.ThreadingHTTPServer):
    def __init__(self, address, handler_class, backends, balancing_algorithm):
        super().__init__(address, handler_class)
        self.backends = backends
        self.response_time_balancer = None
        
        # Initialize the selected load balancer
        if balancing_algorithm == "round_robin":
            self.balancer = RoundRobinBalancer(backends)
        elif balancing_algorithm == "random":
            self.balancer = RandomBalancer(backends)
        elif balancing_algorithm == "least_connections":
            self.balancer = LeastConnectionsBalancer(backends)
        elif balancing_algorithm == "weighted_round_robin":
            self.balancer = WeightedRoundRobinBalancer(backends)
        elif balancing_algorithm == "ip_hash":
            self.balancer = IPHashBalancer(backends)
        elif balancing_algorithm == "least_response_time":
            self.balancer = LeastResponseTimeBalancer(backends)
            self.response_time_balancer = self.balancer  # For tracking response times
        elif balancing_algorithm == "health_check":
            self.balancer = HealthCheckBalancer(backends)
        else:
            logger.error(f"Unknown balancing algorithm: {balancing_algorithm}")
            self.balancer = RoundRobinBalancer(backends)  # Fallback


def parse_arguments():
    parser = argparse.ArgumentParser(description='Reverse proxy server with load balancing')
    parser.add_argument(
        '--port', 
        type=int, 
        default=8000, 
        help='Port to listen on (default: 8000)'
    )
    parser.add_argument(
        '--host', 
        default='localhost', 
        help='Host to bind to (default: localhost)'
    )
    parser.add_argument(
        '--backends', 
        required=True, 
        help='Comma-separated list of backend servers (host:port)'
    )
    parser.add_argument(
        '--algorithm', 
        default='round_robin', 
        choices=[
            'round_robin', 
            'random', 
            'least_connections', 
            'weighted_round_robin', 
            'ip_hash', 
            'least_response_time', 
            'health_check'
        ],
        help='Load balancing algorithm (default: round_robin)'
    )
    return parser.parse_args()


def main():
    args = parse_arguments()
    backends = args.backends.split(',')
    
    # Validate backend format
    for backend in backends:
        if ':' not in backend:
            logger.error(f"Invalid backend format: {backend}. Use host:port format.")
            sys.exit(1)
    
    server_address = (args.host, args.port)
    
    try:
        server = ReverseProxyServer(
            server_address, 
            ReverseProxyHandler, 
            backends, 
            args.algorithm
        )
        
        logger.info(f"Starting reverse proxy server on {args.host}:{args.port}")
        logger.info(f"Using load balancing algorithm: {args.algorithm}")
        logger.info(f"Backends: {backends}")
        
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down server...")
            server.shutdown()
    except Exception as e:
        logger.error(f"Failed to start server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
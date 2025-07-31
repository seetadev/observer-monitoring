# Observer Monitoring System

A comprehensive real-time monitoring tool for tracking the status and performance of local and remote servers. Built with Python, this system provides live system metrics, statistical analysis, and performance testing capabilities through an intuitive web dashboard.

## 🚀 Features

- **Real-time Server Monitoring** - Live tracking of CPU, memory, disk, and network usage
- **Multi-Server Support** - Monitor multiple remote servers from a centralized dashboard
- **Interactive Web Dashboard** - Modern web interface with real-time charts and visualizations
- **Performance Testing** - Built-in tools for server performance analysis
- **Statistical Analysis** - Historical data tracking and trend analysis
- **Cost Monitoring** - Resource usage cost tracking and optimization
- **TCP-based Communication** - Efficient client-server communication protocol
- **Network Interface Selection** - Flexible network adapter configuration
- **Live Data Streaming** - Real-time data updates using WebSocket connections

## 🛠️ Tech Stack

### Backend
- **Python** - Core application logic
- **Tornado** - Asynchronous web framework and HTTP server
- **psutil** - System and process monitoring library
- **MongoDB** - Database for storing historical metrics and statistics
- **TCP Sockets** - Client-server communication

### Frontend
- **HTML5/CSS3** - Modern web interface
- **JavaScript (ES5)** - Interactive client-side functionality
- **Bootstrap 3** - Responsive UI framework
- **jQuery 2.2.0** - DOM manipulation and AJAX
- **Chart.js** - Data visualization and charting
- **ZingChart** - Advanced charting library
- **Smoothie.js** - Real-time streaming charts
- **CodeMirror** - Code editor integration
- **Font Awesome** - Icon library

### Development Tools
- **npm** - Package management
- **PhantomJS** - Headless browser testing
- **Bash Scripts** - Automation and deployment

## 📁 Project Structure

```
observer-monitoring/
├── static/                 # Frontend assets
│   ├── css/               # Stylesheets and themes
│   └── js/                # JavaScript libraries and modules
├── templates/             # HTML templates
├── fonts/                 # Font files (FontAwesome, Bootstrap)
├── run.py                 # Main application entry point
├── root.py                # Root server implementation
├── remote.py              # Remote client implementation
├── observerDriver.py      # Core monitoring driver
├── uimodules.py          # UI module definitions
├── run-root.sh           # Root server startup script
├── run-remote.sh         # Remote client startup script
└── install_phantomjs.sh  # PhantomJS installation script
```

## 🔧 Installation & Setup

### Prerequisites
- Python 2.7+ or Python 3.x
- MongoDB server
- Node.js and npm (for package management)
- Network connectivity between monitored servers

### Installation Steps

1. **Clone the repository**
```bash
git clone https://github.com/anisharma07/observer-monitoring.git
cd observer-monitoring
```

2. **Install Python dependencies**
```bash
pip install tornado psutil pymongo
```

3. **Install PhantomJS (optional, for testing)**
```bash
bash install_phantomjs.sh
```

4. **Start MongoDB service**
```bash
# On Ubuntu/Debian
sudo service mongod start

# On macOS with Homebrew
brew services start mongodb-community
```

5. **Make scripts executable**
```bash
chmod +x run-root.sh run-remote.sh install_phantomjs.sh close-all.sh
```

## 🎯 Usage

### Root Server Setup

Set up the central monitoring server on your main machine:

```bash
bash run-root.sh
```

This command starts multiple services:
- **TCP Listener** (Port 8889) - Receives data from remote clients
- **Dashboard Web Server** (Port 9000) - Main monitoring interface
- **Simulation Server** (Port 9001) - Performance testing
- **Live Monitor Server** (Port 9002) - Real-time data streaming
- **Compute Server** (Port 9003) - Data processing

### Remote Client Setup

Deploy monitoring clients on servers you want to monitor:

```bash
bash run-remote.sh
```

Follow the interactive prompts:
- **Destination IP**: IP address of your root server
- **Destination Port**: TCP port (default: 8889)
- **Remote Name**: Identifier for this server
- **Preferred NIC**: Network interface (e.g., eth0, wlan0)

### Dashboard Access

Access the monitoring dashboards through your web browser:

- **Live Monitoring**: `http://[ROOT_IP]:9000/live`
- **Statistical Data**: `http://[ROOT_IP]:9000/stat`  
- **Performance Testing**: `http://[ROOT_IP]:9000/perf`
- **Cost Analysis**: `http://[ROOT_IP]:9000/cost`
- **Admin Panel**: `http://[ROOT_IP]:9000/admin`

### Manual Service Control

Run individual services manually:

```bash
# TCP Listener
python run.py listener 8889

# Dashboard server
python run.py dash 9000

# Remote client
python run.py remote 8888
```

## 📱 Platform Support

- **Linux** - Primary platform support
- **macOS** - Compatible with macOS systems
- **Windows** - Windows compatibility with minor modifications
- **Docker** - Containerization ready
- **Cloud Platforms** - AWS, GCP, Azure compatible

## 🧪 Testing

The project includes testing infrastructure:

```bash
# Run phantom testing (requires PhantomJS)
./install_phantomjs.sh

# Manual testing
python run.py test
```

## 🔄 Deployment

### Production Deployment

1. **Configure firewall rules**
```bash
# Allow required ports
sudo ufw allow 8889  # TCP Listener
sudo ufw allow 9000  # Dashboard
sudo ufw allow 9001  # Simulation
sudo ufw allow 9002  # Live Monitor
```

2. **Set up as system service** (Linux)
```bash
# Create systemd service file
sudo nano /etc/systemd/system/observer-monitoring.service
sudo systemctl enable observer-monitoring
sudo systemctl start observer-monitoring
```

3. **Configure MongoDB for production**
```bash
# Enable authentication and configure replica sets
mongo --eval "db.createUser({user: 'observer', pwd: 'password', roles: ['readWrite']})"
```

### Docker Deployment

```bash
# Build Docker image
docker build -t observer-monitoring .

# Run container
docker run -d -p 9000:9000 -p 8889:8889 observer-monitoring
```

## 📊 Performance & Optimization

- **Real-time Data**: Optimized TCP communication for minimal latency
- **Efficient Storage**: MongoDB aggregation for historical data analysis
- **Scalable Architecture**: Supports monitoring hundreds of remote servers
- **Memory Management**: Efficient memory usage with psutil optimizations
- **Network Optimization**: Configurable network interface selection

## 🤝 Contributing

We welcome contributions! Please follow these guidelines:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Development Guidelines

- Follow PEP 8 coding standards for Python
- Test your changes on multiple platforms
- Update documentation for new features
- Ensure backward compatibility
- Add unit tests for new functionality

## 📄 License

This project is licensed under the ISC License. See the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- **psutil** - Cross-platform system monitoring library
- **Tornado** - Scalable, non-blocking web server
- **MongoDB** - NoSQL database for metrics storage
- **Chart.js** - Beautiful and responsive charts
- **Bootstrap** - Responsive web design framework

## 📞 Support & Contact

- **Issues**: [GitHub Issues](https://github.com/anisharma07/observer-monitoring/issues)
- **Repository**: [anisharma07/observer-monitoring](https://github.com/anisharma07/observer-monitoring)
- **Documentation**: Check the `templates/` directory for usage examples

For technical support or questions about deployment, please open an issue on GitHub with detailed information about your setup and the problem you're experiencing.

---

**Note**: This monitoring system is designed for production environments. Ensure proper security measures are in place when deploying across networks, including firewall configuration and authentication mechanisms.
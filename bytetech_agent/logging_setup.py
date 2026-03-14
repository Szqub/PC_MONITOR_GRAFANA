import logging
import os
from logging.handlers import RotatingFileHandler
from bytetech_agent.config import LoggingConfig

def setup_logging(config: LoggingConfig):
    log_level = getattr(logging, config.level.upper(), logging.INFO)
    
    if not os.path.exists(config.log_dir):
        os.makedirs(config.log_dir, exist_ok=True)
        
    log_file = os.path.join(config.log_dir, "bytetech_agent.log")
    
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
    )
    
    # File rotation (max 10 MB, keep 5 backups)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # Prevent duplicate handlers on restart
    root_logger.handlers.clear()
    
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logging.getLogger("bytetech_agent").info("Logging system initialized.")

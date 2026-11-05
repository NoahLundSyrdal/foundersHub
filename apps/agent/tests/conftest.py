"""Offline test configuration: never use developer provider credentials."""
import os
os.environ["OFFICE_MODEL"] = "mock"
os.environ["TAVILY_API_KEY"] = ""
os.environ["ODA_FOOD_ENABLED"] = "false"

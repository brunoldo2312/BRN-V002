"""Pacote de contratos declarativos do BRN.

A VM é experimental e não equivale a execução EVM nem, isoladamente,
a execução determinística validada por consenso.
"""
from .vm import ContractVM, deploy, call

__all__ = ["ContractVM", "deploy", "call"]

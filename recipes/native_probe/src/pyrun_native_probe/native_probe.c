#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject *probe_add(PyObject *self, PyObject *args) {
    long left;
    long right;
    if (!PyArg_ParseTuple(args, "ll", &left, &right)) {
        return NULL;
    }
    return PyLong_FromLong(left + right);
}

static PyObject *probe_native_abi(PyObject *self, PyObject *Py_UNUSED(ignored)) {
    return PyUnicode_FromString("CPython Android native extension loaded");
}

static PyMethodDef probe_methods[] = {
    {"add", probe_add, METH_VARARGS, "Add two integers in a compiled extension."},
    {"native_abi", probe_native_abi, METH_NOARGS, "Return a marker proving the extension loaded."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef probe_module = {
    PyModuleDef_HEAD_INIT,
    "_native",
    "PyRun Android native wheel pipeline probe.",
    -1,
    probe_methods,
};

PyMODINIT_FUNC PyInit__native(void) {
    return PyModule_Create(&probe_module);
}

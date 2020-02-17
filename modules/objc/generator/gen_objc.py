#!/usr/bin/env python

import sys, re, os.path, errno, fnmatch
import json
import logging
import codecs
from shutil import copyfile
from pprint import pformat
from string import Template

if sys.version_info[0] >= 3:
    from io import StringIO
else:
    import io
    class StringIO(io.StringIO):
        def write(self, s):
            if isinstance(s, str):
                s = unicode(s)  # noqa: F821
            return super(StringIO, self).write(s)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# list of modules + files remap
config = None
ROOT_DIR = None
FILES_REMAP = {}
def checkFileRemap(path):
    path = os.path.realpath(path)
    if path in FILES_REMAP:
        return FILES_REMAP[path]
    assert path[-3:] != '.in', path
    return path

total_files = 0
updated_files = 0

module_imports = []
module_header_code = None
module_body_code = None

# list of class names, which should be skipped by wrapper generator
# the list is loaded from misc/objc/gen_dict.json defined for the module and its dependencies
class_ignore_list = []

# list of constant names, which should be skipped by wrapper generator
# ignored constants can be defined using regular expressions
const_ignore_list = []

# list of private constants
const_private_list = []

# { Module : { public : [[name, val],...], private : [[]...] } }
missing_consts = {}

type_dict = {
    ""        : {"objc_type" : ""}, # c-tor ret_type
    "void"    : {"objc_type" : "void", "is_primitive" : True},
    "bool"    : {"objc_type" : "BOOL", "is_primitive" : True},
    "char"    : {"objc_type" : "char", "is_primitive" : True},
    "int"     : {"objc_type" : "int", "is_primitive" : True, "out_type" : "IntOut*", "out_type_ptr": "%(n)s.ptr", "out_type_ref": "*(int*)(%(n)s.ptr)"},
    "long"    : {"objc_type" : "long", "is_primitive" : True},
    "float"   : {"objc_type" : "float", "is_primitive" : True, "out_type" : "FloatOut*", "out_type_ptr": "%(n)s.ptr", "out_type_ref": "*(float*)(%(n)s.ptr)"},
    "double"  : {"objc_type" : "double", "is_primitive" : True, "out_type" : "DoubleOut*", "out_type_ptr": "%(n)s.ptr", "out_type_ref": "*(double*)(%(n)s.ptr)"},
    "size_t"  : {"objc_type" : "int", "is_primitive" : True},
    "int64"   : {"objc_type" : "long", "is_primitive" : True},
    "string"  : {"objc_type" : "NSString*", "is_primitive" : True}
}

# Defines a rule to add extra prefixes for names from specific namespaces.
# In example, cv::fisheye::stereoRectify from namespace fisheye is wrapped as fisheye_stereoRectify
namespaces_dict = {}

# { class : { func : {declaration, implementation} } }
ManualFuncs = {}

# { class : { func : { arg_name : {"ctype" : ctype, "attrib" : [attrib]} } } }
func_arg_fix = {}

def read_contents(fname):
    with open(fname, 'r') as f:
        data = f.read()
    return data

def mkdir_p(path):
    ''' mkdir -p '''
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise

T_OBJC_CLASS_HEADER = read_contents(os.path.join(SCRIPT_DIR, 'templates/objc_class_header.template'))
T_OBJC_CLASS_BODY = read_contents(os.path.join(SCRIPT_DIR, 'templates/objc_class_body.template'))
T_OBJC_MODULE_HEADER = read_contents(os.path.join(SCRIPT_DIR, 'templates/objc_module_header.template'))
T_OBJC_MODULE_BODY = read_contents(os.path.join(SCRIPT_DIR, 'templates/objc_module_body.template'))

class GeneralInfo():
    def __init__(self, type, decl, namespaces):
        self.namespace, self.classpath, self.classname, self.name = self.parseName(decl[0], namespaces)

        # parse doxygen comments
        self.params={}
        if type == "class":
            docstring="// C++: class " + self.name + "\n"
        else:
            docstring=""

        if len(decl)>5 and decl[5]:
            doc = decl[5]

            docstring += sanitize_documentation_string(doc, type)

        self.docstring = docstring

    def parseName(self, name, namespaces):
        '''
        input: full name and available namespaces
        returns: (namespace, classpath, classname, name)
        '''
        name = name[name.find(" ")+1:].strip() # remove struct/class/const prefix
        spaceName = ""
        localName = name # <classes>.<name>
        for namespace in sorted(namespaces, key=len, reverse=True):
            if name.startswith(namespace + "."):
                spaceName = namespace
                localName = name.replace(namespace + ".", "")
                break
        pieces = localName.split(".")
        if len(pieces) > 2: # <class>.<class>.<class>.<name>
            return spaceName, ".".join(pieces[:-1]), pieces[-2], pieces[-1]
        elif len(pieces) == 2: # <class>.<name>
            return spaceName, pieces[0], pieces[0], pieces[1]
        elif len(pieces) == 1: # <name>
            return spaceName, "", "", pieces[0]
        else:
            return spaceName, "", "" # error?!

    def fullName(self, isCPP=False):
        result = ".".join([self.fullClass(), self.name])
        return result if not isCPP else get_cname(result)

    def fullClass(self, isCPP=False):
        result = ".".join([f for f in [self.namespace] + self.classpath.split(".") if len(f)>0])
        return result if not isCPP else get_cname(result)

class ConstInfo(GeneralInfo):
    def __init__(self, decl, addedManually=False, namespaces=[], enumType=None):
        GeneralInfo.__init__(self, "const", decl, namespaces)
        self.cname = get_cname(self.name)
        self.value = decl[1]
        self.enumType = enumType
        self.addedManually = addedManually
        if self.namespace in namespaces_dict:
            self.name = '%s_%s' % (namespaces_dict[self.namespace], self.name)

    def __repr__(self):
        return Template("CONST $name=$value$manual").substitute(name=self.name,
                                                                 value=self.value,
                                                                 manual="(manual)" if self.addedManually else "")

    def isIgnored(self):
        for c in const_ignore_list:
            if re.match(c, self.name):
                return True
        return False

def normalize_field_name(name):
    return name.replace(".","_").replace("[","").replace("]","").replace("_getNativeObjAddr()","_nativeObj")

def normalize_class_name(name):
    return re.sub(r"^cv\.", "", name).replace(".", "_")

def get_cname(name):
    return name.replace(".", "::")

def cast_from(t):
    if t in type_dict and "cast_from" in type_dict[t]:
        return type_dict[t]["cast_from"]
    return t

def cast_to(t):
    if t in type_dict and "cast_to" in type_dict[t]:
        return type_dict[t]["cast_to"]
    return t

class ClassPropInfo():
    def __init__(self, decl): # [f_ctype, f_name, '', '/RW']
        self.ctype = decl[0]
        self.name = decl[1]
        self.rw = "/RW" in decl[3]

    def __repr__(self):
        return Template("PROP $ctype $name").substitute(ctype=self.ctype, name=self.name)

class ClassInfo(GeneralInfo):
    def __init__(self, decl, namespaces=[]): # [ 'class/struct cname', ': base', [modlist] ]
        GeneralInfo.__init__(self, "class", decl, namespaces)
        self.cname = get_cname(self.name)
        self.methods = []
        self.methods_suffixes = {}
        self.consts = [] # using a list to save the occurrence order
        self.private_consts = []
        self.imports = set()
        self.props= []
        self.objc_name = self.name
        self.smart = None # True if class stores Ptr<T>* instead of T* in nativeObj field
        self.enum_declarations = None # Objective-C enum declarations stream
        self.method_declarations = None # Objective-C method declarations stream
        self.method_implementations = None # Objective-C method implementations stream
        self.objc_header_template = None # Objective-C header code
        self.objc_body_template = None # Objective-C body code
        for m in decl[2]:
            if m.startswith("="):
                self.objc_name = m[1:]
        self.base = ''
        self.is_base_class = True
        if decl[1]:
            self.base = re.sub(r"^.*:", "", decl[1].split(",")[0]).strip().replace(self.objc_name, "")

    def __repr__(self):
        return Template("CLASS $namespace::$classpath.$name : $base").substitute(**self.__dict__)

    def getImports(self, module):
        return ["#import \"%s.h\"" % c for c in sorted(self.imports)]

    def getForwardDeclarations(self, module):
        return ["@class %s;" % c for c in sorted(self.imports)]

    def addImports(self, ctype, is_out_type):
        if ctype in type_dict:
            objc_import = None
            if "v_type" in type_dict[ctype]:
                objc_import = type_dict[type_dict[ctype]["v_type"]]["objc_type"]
            elif "v_v_type" in type_dict[ctype]:
                objc_import = type_dict[type_dict[ctype]["v_v_type"]]["objc_type"]
            elif not type_dict[ctype].get("is_primitive", False) or (is_out_type and type_dict[ctype].get("out_type", "")):
                if is_out_type and type_dict[ctype].get("out_type", ""):
                    objc_import = type_dict[ctype]["out_type"]
                else:
                    objc_import = type_dict[ctype]["objc_type"]
            if objc_import is not None and objc_import not in ["NSNumber*", "NSString*"]:
                self.imports.add(objc_import[:-1] if objc_import[-1] == "*" else objc_import)   # remove trailing "*"

    def getAllMethods(self):
        result = []
        result.extend([fi for fi in sorted(self.methods) if fi.isconstructor])
        result.extend([fi for fi in sorted(self.methods) if not fi.isconstructor])
        return result

    def addMethod(self, fi):
        self.methods.append(fi)

    def getConst(self, name):
        for cand in self.consts + self.private_consts:
            if cand.name == name:
                return cand
        return None

    def addConst(self, constinfo):
        # choose right list (public or private)
        consts = self.consts
        for c in const_private_list:
            if re.match(c, constinfo.name):
                consts = self.private_consts
                break
        consts.append(constinfo)

    def initCodeStreams(self, Module):
        self.enum_declarations = StringIO()
        self.method_declarations = StringIO()
        self.method_implementations = StringIO()
        if self.base:
            self.objc_header_template = T_OBJC_CLASS_HEADER
            self.objc_body_template = T_OBJC_CLASS_BODY
            self.is_base_class = False
        else:
            self.base = "NSObject"
            if self.name != Module:
                self.objc_header_template = T_OBJC_CLASS_HEADER
                self.objc_body_template = T_OBJC_CLASS_BODY
            else:
                self.objc_header_template = T_OBJC_MODULE_HEADER
                self.objc_body_template = T_OBJC_MODULE_BODY
        # misc handling
        if self.name == Module:
          for i in module_imports or []:
              self.imports.add(i)

    def cleanupCodeStreams(self):
        self.enum_declarations.close()
        self.method_declarations.close()
        self.method_implementations.close()

    def generateObjcHeaderCode(self, m, M):
        return Template(self.objc_header_template + "\n\n").substitute(
                            module = M,
                            importBaseClass = '#import "' + self.base + '.h"' if not self.is_base_class else "",
                            forwardDeclarations = "\n".join(self.getForwardDeclarations(M)),
                            enumDeclarations = self.enum_declarations.getvalue(),
                            moduleHeaderCode = module_header_code,
                            nativePointerHandling = Template(
"""
#ifdef __cplusplus
@property(readonly)cv::$cName* nativePtr;
#endif

- (void)dealloc;

#ifdef __cplusplus
- (instancetype)initWithNativePtr:(cv::$cName*)nativePtr;
+ (instancetype)fromNative:(cv::$cName*)nativePtr;
#endif
"""
                            ).substitute(
                                cName = self.cname
                            ) if self.is_base_class else "",
                            manualMethodDeclations = "",
                            methodDeclarations = self.method_declarations.getvalue(),
                            name = self.name,
                            objcName = self.objc_name,
                            cName = self.cname,
                            imports = "\n".join(self.getImports(M)),
                            docs = self.docstring,
                            base = self.base)

    def generateObjcBodyCode(self, m, M):
        return Template(self.objc_body_template + "\n\n").substitute(
                            module = M,
                            moduleBodyCode = module_body_code,
                            nativePointerHandling=Template(
"""
- (void)dealloc {
    if (_nativePtr != NULL) {
        delete _nativePtr;
    }
}

- (instancetype)initWithNativePtr:(cv::$cName*)nativePtr {
    self = [super init];
    if (self) {
        _nativePtr = nativePtr;
    }
    return self;
}

+ (instancetype)fromNative:(cv::$cName*)nativePtr {
    return [[$objcName alloc] initWithNativePtr:nativePtr];
}
"""
                            ).substitute(
                                cName=self.cname,
                                objcName=self.objc_name
                            ) if self.is_base_class else "",
                            manualMethodDeclations = "",
                            methodImplementations = self.method_implementations.getvalue(),
                            name = self.name,
                            objcName = self.objc_name,
                            cName = self.cname,
                            imports = "\n".join(self.getImports(M)),
                            docs = self.docstring,
                            base = self.base)

class ArgInfo():
    def __init__(self, arg_tuple): # [ ctype, name, def val, [mod], argno ]
        self.pointer = False
        ctype = arg_tuple[0]
        if ctype.endswith("*"):
            ctype = ctype[:-1]
            self.pointer = True
        self.ctype = ctype
        self.name = arg_tuple[1]
        self.defval = arg_tuple[2]
        self.out = ""
        if "/O" in arg_tuple[3]:
            self.out = "O"
        if "/IO" in arg_tuple[3]:
            self.out = "IO"

    def __repr__(self):
        return Template("ARG $ctype$p $name=$defval").substitute(ctype=self.ctype,
                                                                  p=" *" if self.pointer else "",
                                                                  name=self.name,
                                                                  defval=self.defval)

class FuncInfo(GeneralInfo):
    def __init__(self, decl, namespaces=[]): # [ funcname, return_ctype, [modifiers], [args] ]
        GeneralInfo.__init__(self, "func", decl, namespaces)
        self.cname = get_cname(decl[0])
        self.objc_name = self.name
        self.swift_name = self.name
        self.cv_name = self.fullName(isCPP=True)
        self.isconstructor = self.name == self.classname
        if "[" in self.name:
            self.objc_name = "getelem"
        if self.namespace in namespaces_dict:
            self.objc_name = '%s_%s' % (namespaces_dict[self.namespace], self.objc_name)
        for m in decl[2]:
            if m.startswith("="):
                self.objc_name = m[1:]
        self.static = ["","static"][ "/S" in decl[2] ]
        self.ctype = re.sub(r"^CvTermCriteria", "TermCriteria", decl[1] or "")
        self.args = []
        func_fix_map = func_arg_fix.get(self.objc_name, {})
        for a in decl[3]:
            arg = a[:]
            arg_fix_map = func_fix_map.get(arg[1], {})
            arg[0] = arg_fix_map.get('ctype',  arg[0]) #fixing arg type
            arg[3] = arg_fix_map.get('attrib', arg[3]) #fixing arg attrib
            self.args.append(ArgInfo(arg))

        func_fix_map = func_arg_fix.get(self.signature(self.args), {})
        name_fix_map = func_fix_map.get(self.name, {})
        self.objc_name = name_fix_map.get('name', self.objc_name)
        for arg in self.args:
            arg_fix_map = func_fix_map.get(arg.name, {})
            arg.ctype = arg_fix_map.get('ctype', arg.ctype) #fixing arg type
            arg.name = arg_fix_map.get('name', arg.name) #fixing arg name

    def __repr__(self):
        return Template("FUNC <$ctype $namespace.$classpath.$name $args>").substitute(**self.__dict__)

    def __lt__(self, other):
        return self.__repr__() < other.__repr__()

    def signature(self, args):
        objc_args = build_objc_args(args)
        return "(" + type_dict[self.ctype]["objc_type"] + ")" + self.objc_name + " ".join(objc_args)

def type_complete(args):
    for a in args:
        if a.ctype not in type_dict:
            if not a.defval and a.ctype.endswith("*"):
                a.defval = 0
            if a.defval:
                a.ctype = ''
                a.defval = a.defval.replace("Ptr<", "cv::Ptr<")
                continue
            return False
    return True

def build_objc_args(args):
    objc_args = []
    if type_complete(args):
        for a in args:
            if a.ctype not in type_dict:
                if not a.defval and a.ctype.endswith("*"):
                    a.defval = 0
                if a.defval:
                    a.ctype = ''
                    continue
            if not a.ctype:  # hidden
                continue
            objc_type = type_dict[a.ctype]["objc_type"]
            if "v_type" in type_dict[a.ctype]:
                if "O" in a.out:
                    objc_type = "NSMutableArray<" + objc_type + ">*"
                else:
                    objc_type = "NSArray<" + objc_type + ">*"
            elif "v_v_type" in type_dict[a.ctype]:
                if "O" in a.out:
                    objc_type = "NSMutableArray<NSMutableArray<" + objc_type + ">*>*"
                else:
                    objc_type = "NSArray<NSArray<" + objc_type + ">*>*"

            if a.out and type_dict[a.ctype].get("out_type", ""):
                objc_type = type_dict[a.ctype]["out_type"]
            objc_args.append((a.name if len(objc_args) > 0 else '') + ':(' + objc_type + ')' + a.name)
    return objc_args

def build_swift_signature(args):
    swift_signature = ""
    if type_complete(args):
        for a in args:
            if a.ctype not in type_dict:
                if not a.defval and a.ctype.endswith("*"):
                    a.defval = 0
                if a.defval:
                    a.ctype = ''
                    continue
            if not a.ctype:  # hidden
                continue
            swift_signature += a.name + ":"
    return swift_signature

class ObjectiveCWrapperGenerator(object):
    def __init__(self):
        self.clear()

    def clear(self):
        self.namespaces = set(["cv"])
        self.classes = { "Mat" : ClassInfo([ 'class Mat', '', [], [] ], self.namespaces) }
        self.module = ""
        self.Module = ""
        self.ported_func_list = []
        self.skipped_func_list = []
        self.def_args_hist = {} # { def_args_cnt : funcs_cnt }

    def add_class(self, decl):
        classinfo = ClassInfo(decl, namespaces=self.namespaces)
        if classinfo.name in class_ignore_list:
            logging.info('ignored: %s', classinfo)
            return
        name = classinfo.name
        if self.isWrapped(name) and not classinfo.base:
            logging.warning('duplicated: %s', classinfo)
            return
        self.classes[name] = classinfo
        if name in type_dict and not classinfo.base:
            logging.warning('duplicated: %s', classinfo)
            return
        type_dict.setdefault(name, {}).update(
            { "objc_type" : classinfo.objc_name + "*"}
        )

        # missing_consts { Module : { public : [[name, val],...], private : [[]...] } }
        if name in missing_consts:
            if 'public' in missing_consts[name]:
                for (n, val) in missing_consts[name]['public']:
                    classinfo.consts.append( ConstInfo([n, val], addedManually=True) )

        # class props
        for p in decl[3]:
            if True: #"vector" not in p[0]:
                classinfo.props.append( ClassPropInfo(p) )
            else:
                logging.warning("Skipped property: [%s]" % name, p)

        type_dict.setdefault("Ptr_"+name, {}).update(
            { "objc_type" : classinfo.objc_name + "*",
              "c_type" : name,
              "from_cpp_ptr": "[" + name + " fromNativePtr:%(n)s]"}
        )
        logging.info('ok: class %s, name: %s, base: %s', classinfo, name, classinfo.base)

    def add_const(self, decl, scope=None, enumType=None): # [ "const cname", val, [], [] ]
        constinfo = ConstInfo(decl, namespaces=self.namespaces, enumType=enumType)
        if constinfo.isIgnored():
            logging.info('ignored: %s', constinfo)
        else:
            if not self.isWrapped(constinfo.classname):
                logging.info('class not found: %s', constinfo)
                constinfo.name = constinfo.classname + '_' + constinfo.name
                constinfo.classname = ''

            ci = self.getClass(constinfo.classname)
            duplicate = ci.getConst(constinfo.name)
            back_ref = ci.getConst(constinfo.value)
            if back_ref and not enumType:
                constinfo.value = scope + "." + constinfo.value
            if duplicate:
                if duplicate.addedManually:
                    logging.info('manual: %s', constinfo)
                else:
                    logging.warning('duplicated: %s', constinfo)
            else:
                ci.addConst(constinfo)
                logging.info('ok: %s', constinfo)

    def add_enum(self, decl, scope): # [ "enum cname", "", [], [] ]
        enumType = decl[0].rsplit(" ", 1)[1]
        if enumType.endswith("<unnamed>"):
            enumType = None
        else:
            ctype = normalize_class_name(enumType)
            type_dict[ctype] = { "cast_from" : "int", "cast_to" : get_cname(enumType), "objc_type" : "int" }
        const_decls = decl[3]

        for decl in const_decls:
            self.add_const(decl, scope, enumType)

    def add_func(self, decl):
        fi = FuncInfo(decl, namespaces=self.namespaces)
        classname = fi.classname or self.Module
        if classname in class_ignore_list:
            logging.info('ignored: %s', fi)
        elif classname in ManualFuncs and fi.objc_name in ManualFuncs[classname]:
            logging.info('manual: %s', fi)
        elif not self.isWrapped(classname):
            logging.warning('not found: %s', fi)
        else:
            self.getClass(classname).addMethod(fi)
            logging.info('ok: %s', fi)
            # calc args with def val
            cnt = len([a for a in fi.args if a.defval])
            self.def_args_hist[cnt] = self.def_args_hist.get(cnt, 0) + 1

    def save(self, path, buf):
        global total_files, updated_files
        total_files += 1
        if os.path.exists(path):
            with open(path, "rt") as f:
                content = f.read()
                if content == buf:
                    return
        with codecs.open(path, "w", "utf-8") as f:
            f.write(buf)
        updated_files += 1

    def gen(self, srcfiles, module, output_path, output_objc_path, common_headers):
        self.clear()
        self.module = module
        self.Module = module.capitalize()
        # TODO: support UMat versions of declarations (implement UMat-wrapper for Java)
        parser = hdr_parser.CppHeaderParser(generate_umat_decls=False)

        self.add_class( ['class ' + self.Module, '', [], []] ) # [ 'class/struct cname', ':bases', [modlist] [props] ]

        # scan the headers and build more descriptive maps of classes, consts, functions
        includes = []
        for hdr in common_headers:
            logging.info("\n===== Common header : %s =====", hdr)
            includes.append('#include "' + hdr + '"')
        for hdr in srcfiles:
            decls = parser.parse(hdr)
            self.namespaces = parser.namespaces
            logging.info("\n\n===== Header: %s =====", hdr)
            logging.info("Namespaces: %s", parser.namespaces)
            if decls:
                includes.append('#include "' + hdr + '"')
            else:
                logging.info("Ignore header: %s", hdr)
            for decl in decls:
                logging.info("\n--- Incoming ---\n%s", pformat(decl[:5], 4)) # without docstring
                name = decl[0]
                if name.startswith("struct") or name.startswith("class"):
                    self.add_class(decl)
                elif name.startswith("const"):
                    self.add_const(decl)
                elif name.startswith("enum"):
                    # enum
                    self.add_enum(decl, self.Module)
                else: # function
                    self.add_func(decl)

        logging.info("\n\n===== Generating... =====")
        package_path = os.path.join(output_objc_path, module)
        mkdir_p(package_path)
        for ci in self.classes.values():
            if ci.name == "Mat":
                continue
            ci.initCodeStreams(self.Module)
            self.gen_class(ci)
            classObjcHeaderCode = ci.generateObjcHeaderCode(self.module, self.Module)
            self.save("%s/%s/%s.h" % (output_objc_path, module, ci.objc_name), classObjcHeaderCode)
            classObjcBodyCode = ci.generateObjcBodyCode(self.module, self.Module)
            self.save("%s/%s/%s.mm" % (output_objc_path, module, ci.objc_name), classObjcBodyCode)
            ci.cleanupCodeStreams()
        self.save(os.path.join(output_path, module+".txt"), self.makeReport())

    def makeReport(self):
        '''
        Returns string with generator report
        '''
        report = StringIO()
        total_count = len(self.ported_func_list)+ len(self.skipped_func_list)
        report.write("PORTED FUNCs LIST (%i of %i):\n\n" % (len(self.ported_func_list), total_count))
        report.write("\n".join(self.ported_func_list))
        report.write("\n\nSKIPPED FUNCs LIST (%i of %i):\n\n" % (len(self.skipped_func_list), total_count))
        report.write("".join(self.skipped_func_list))
        for i in self.def_args_hist.keys():
            report.write("\n%i def args - %i funcs" % (i, self.def_args_hist[i]))
        return report.getvalue()

    def fullTypeName(self, t):
        if not type_dict[t].get("is_primitive", False):
            return "cv::" + t
        else:
            return t

    def gen_func(self, ci, fi, prop_name=''):
        logging.info("%s", fi)
        method_declarations = ci.method_declarations
        method_implementations = ci.method_implementations

        # c_decl
        # e.g: void add(Mat src1, Mat src2, Mat dst, Mat mask = Mat(), int dtype = -1)
        if prop_name:
            c_decl = "%s %s::%s" % (fi.ctype, fi.classname, prop_name)
        else:
            decl_args = []
            for a in fi.args:
                s = a.ctype or ' _hidden_ '
                if a.pointer:
                    s += "*"
                elif a.out:
                    s += "&"
                s += " " + a.name
                if a.defval:
                    s += " = " + a.defval
                decl_args.append(s)
            c_decl = "%s %s %s(%s)" % ( fi.static, fi.ctype, fi.cname, ", ".join(decl_args) )

        # comment
        method_declarations.write( "\n//\n// %s\n//\n" % c_decl )
        method_implementations.write( "\n//\n// %s\n//\n" % c_decl )
        # check if we 'know' all the types
        if fi.ctype not in type_dict: # unsupported ret type
            msg = "// Return type '%s' is not supported, skipping the function\n\n" % fi.ctype
            self.skipped_func_list.append(c_decl + "\n" + msg)
            method_declarations.write( " "*4 + msg )
            logging.warning("SKIP:" + c_decl.strip() + "\t due to RET type " + fi.ctype)
            return
        for a in fi.args:
            if a.ctype not in type_dict:
                if not a.defval and a.ctype.endswith("*"):
                    a.defval = 0
                if a.defval:
                    a.ctype = ''
                    continue
                msg = "// Unknown type '%s' (%s), skipping the function\n\n" % (a.ctype, a.out or "I")
                self.skipped_func_list.append(c_decl + "\n" + msg)
                method_declarations.write( msg )
                logging.warning("SKIP:" + c_decl.strip() + "\t due to ARG type " + a.ctype + "/" + (a.out or "I"))
                return

        self.ported_func_list.append(c_decl)

        # args
        args = fi.args[:] # copy
        objc_signatures=[]
        while True:
             # method args
            cv_args = []
            prologue = []
            epilogue = []
            if fi.ctype:
                ci.addImports(fi.ctype, False)
            for a in args:
                if not "v_type" in type_dict[a.ctype] and not "v_v_type" in type_dict[a.ctype]:
                    cv_name = type_dict[a.ctype].get("to_cpp", "%(n)s") if a.ctype else a.defval
                    if a.pointer:
                        cv_name = "&(" + cv_name + ")"
                    if "O" in a.out and type_dict[a.ctype].get("out_type", ""):
                        cv_name = type_dict[a.ctype].get("out_type_ptr" if a.pointer else "out_type_ref", "%(n)s")
                    cv_args.append(type_dict[a.ctype].get("cv_name", cv_name) % {"n": a.name})
                    if not a.ctype: # hidden
                        continue
                    ci.addImports(a.ctype, "O" in a.out)
                if "v_type" in type_dict[a.ctype]: # pass as vector
                    vector_cpp_type = type_dict[a.ctype]["v_type"]
                    objc_type = type_dict[a.ctype]["objc_type"]
                    ci.addImports(a.ctype, False)
                    vector_full_cpp_type = self.fullTypeName(vector_cpp_type)
                    vector_cpp_name = a.name + "Vector"
                    cv_args.append(vector_cpp_name)
                    prologue.append("OBJC2CV(" + vector_full_cpp_type + ", " + objc_type[:-1] + ", " + vector_cpp_name + ", " + a.name +  ");")
                    if "O" in a.out:
                        epilogue.append(
                            "CV2OBJC(" + vector_full_cpp_type + ", " + objc_type[:-1] + ", " + vector_cpp_name + ", " + a.name + ");")
                if "v_v_type" in type_dict[a.ctype]: # pass as vector of vector
                    vector_cpp_type = type_dict[a.ctype]["v_v_type"]
                    objc_type = type_dict[a.ctype]["objc_type"]
                    ci.addImports(a.ctype, False)
                    vector_full_cpp_type = self.fullTypeName(vector_cpp_type)
                    vector_cpp_name = a.name + "Vector2"
                    cv_args.append(vector_cpp_name)
                    prologue.append("OBJC2CV2(" + vector_full_cpp_type + ", " + objc_type[:-1] + ", " + vector_cpp_name + ", " + a.name +  ");")
                    if "O" in a.out:
                        epilogue.append(
                            "CV2OBJC2(" + vector_full_cpp_type + ", " + objc_type[:-1] + ", " + vector_cpp_name + ", " + a.name + ");")

            # calculate method signature to check for uniqueness
            objc_args = build_objc_args(args)
            objc_signature = fi.signature(args)
            logging.info("Objective-C: " + objc_signature)

            if objc_signature in objc_signatures:
                if args:
                    args.pop()
                    continue
                else:
                    break

            # doc comment
            if fi.docstring:
                lines = fi.docstring.splitlines()
                toWrite = []
                for index, line in enumerate(lines):
                    p0 = line.find("@param")
                    if p0 != -1:
                        p0 += 7
                        p1 = line.find(' ', p0)
                        p1 = len(line) if p1 == -1 else p1
                        name = line[p0:p1]
                        for arg in args:
                            if arg.name == name:
                                toWrite.append(line)
                                break
                    else:
                        toWrite.append(line)

                for line in toWrite:
                    method_declarations.write(line + "\n")

            # public wrapper method impl (calling native one above)
            # e.g.
            # public static void add( Mat src1, Mat src2, Mat dst, Mat mask, int dtype )
            # { add_0( src1.nativeObj, src2.nativeObj, dst.nativeObj, mask.nativeObj, dtype );  }
            ret_type = fi.ctype
            if fi.ctype.endswith('*'):
                ret_type = ret_type[:-1]
            ret_val = self.fullTypeName(fi.ctype) + " retVal = "
            ret = "return retVal;"
            tail = ""
            constructor = False
            if "v_type" in type_dict[ret_type]:
                objc_type = type_dict[ret_type]["objc_type"]
                if type_dict[ret_type]["v_type"] in ("Mat", "vector_Mat"):
                    if objc_type.startswith('MatOf'):
                        ret_val += objc_type + ".fromNativeAddr("
                    else:
                        ret_val = "Mat retValMat = new Mat("
                        prologue.append( j_type + ' retVal = new Array' + j_type+'();')
                        epilogue.append('Converters.Mat_to_' + ret_type + '(retValMat, retVal);')
                        ret = "return retVal;"
            elif ret_type.startswith("Ptr_"):
                cv_type = type_dict[ret_type]["c_type"]
                ret_val = "cv::" + cv_type + "* retVal = "
                ret = "return [" + type_dict[ret_type]["objc_type"][:-1] + " fromNative:retVal];"
            elif ret_type == "void":
                ret_val = ""
                ret = ""
            elif ret_type == "": # c-tor
                constructor = True
                ret_val = "return [self initWithNativePtr:new "
                tail = "]"
                ret = ""
            elif self.isWrapped(ret_type): # wrapped class
                ret_val = "cv::" + ret_type + "* retVal = new cv::" + ret_type + "("
                tail = ")"
                ret = "return " + (type_dict[ret_type]["from_cpp_ptr"] % { "n" : "retVal" }) + ";"
            elif "from_cpp" in type_dict[ret_type]:
                ret = "return " + (type_dict[ret_type]["from_cpp"] % { "n" : "retVal" }) + ";"

            static = True
            if fi.classname:
                static = fi.static

            prototype = Template("$static ($objc_type)$objc_name$objc_args").substitute(
                    static = "+" if static else "-",
                    objc_type = type_dict[fi.ctype]["objc_type"] if type_dict[fi.ctype]["objc_type"] else "void" if not constructor else "instancetype",
                    objc_args = " ".join(objc_args),
                    objc_name = fi.objc_name if not constructor else ("init" + ("With" + args[0].name.capitalize() if len(args) > 0 else ""))
                )

            method_declarations.write( Template(
"""$prototype$swift_name;

"""
                ).substitute(
                    prototype = prototype,
                    swift_name = " NS_SWIFT_NAME(" + fi.swift_name + "(" + build_swift_signature(args) + "))" if not constructor else ""
                )
            )

            method_implementations.write( Template(
"""$prototype {$prologue
    $ret_val$obj_deref$cv_name($cv_args)$tail;$epilogue$ret
}

"""
                ).substitute(
                    prototype = prototype,
                    ret = "\n    " + ret if ret else "",
                    ret_val = ret_val,
                    prologue = "\n    " + "\n    ".join(prologue) if prologue else "",
                    epilogue = "\n    " + "\n    ".join(epilogue) if epilogue else "",
                    static = "+" if static else "-",
                    obj_deref =  ("((" + fi.fullClass(isCPP=True) + "*)self.nativePtr)->" if not ci.is_base_class else "_nativePtr->") if not static and not constructor else "",
                    cv_name = fi.cv_name if static else fi.fullClass(isCPP=True) if constructor else fi.name,
                    cv_args = ", ".join(cv_args),
                    tail = tail
                )
            )
            # adding method signature to dictionary
            objc_signatures.append(objc_signature)

            # processing args with default values
            if args and args[-1].defval:
                args.pop()
            else:
                break

    def gen_class(self, ci):
        logging.info("%s", ci)
        # constants
        consts_map = {c.name: c for c in ci.private_consts}
        consts_map.update({c.name: c for c in ci.consts})
        def const_value(v):
            if v in consts_map:
                target = consts_map[v]
                assert target.value != v
                return const_value(target.value)
            return v
        if ci.consts:
            enumTypes = set(map(lambda c: c.enumType, ci.consts))
            grouped_consts = {enumType: [c for c in ci.consts if c.enumType == enumType] for enumType in enumTypes}
            for typeName, consts in grouped_consts.items():
                logging.info("%s", consts)
                if typeName:
                    typeName = typeName.rsplit(".", 1)[-1]
                    ci.enum_declarations.write("""
// C++: enum {1}
typedef NS_ENUM(int, {2}) {{
    {0}\n}};\n\n""".format(",\n    ".join(["%s = %s" % (c.name, c.value) for c in consts]), typeName, typeName)
                    )
                else:
                    ci.method_declarations.write("""
{0}\n\n""".format("\n".join(["@property (class, readonly) int %s NS_SWIFT_NAME(%s);" % (c.name, c.name) for c in consts]))
                    )
                    ci.method_implementations.write("""
{0}\n\n""".format("\n".join(["+ (int)%s {\n    return %s;\n}\n" % (c.name, c.value) for c in consts]))
                    )
        # methods
        for fi in ci.getAllMethods():
            self.gen_func(ci, fi)
        # props
        for pi in ci.props:
            # getter
            getter_name = ci.fullName() + ".get_" + pi.name
            fi = FuncInfo( [getter_name, pi.ctype, [], []], self.namespaces ) # [ funcname, return_ctype, [modifiers], [args] ]
            self.gen_func(ci, fi, pi.name)
            if pi.rw:
                #setter
                setter_name = ci.fullName() + ".set_" + pi.name
                fi = FuncInfo( [ setter_name, "void", [], [ [pi.ctype, pi.name, "", [], ""] ] ], self.namespaces)
                self.gen_func(ci, fi, pi.name)

        # manual ports
        if ci.name in ManualFuncs:
            for func in ManualFuncs[ci.name].keys():
                ci.method_declarations.write( "\n".join(ManualFuncs[ci.name][func]["declaration"]) )
                ci.method_implementations.write( "\n".join(ManualFuncs[ci.name][func]["implementation"]) )

    def getClass(self, classname):
        return self.classes[classname or self.Module]

    def isWrapped(self, classname):
        name = classname or self.Module
        return name in self.classes

    def isSmartClass(self, ci):
        '''
        Check if class stores Ptr<T>* instead of T* in nativeObj field
        '''
        if ci.smart != None:
            return ci.smart

        # if parents are smart (we hope) then children are!
        # if not we believe the class is smart if it has "create" method
        ci.smart = False
        if ci.base or ci.name == 'Algorithm':
            ci.smart = True
        else:
            for fi in ci.methods:
                if fi.name == "create":
                    ci.smart = True
                    break

        return ci.smart

    def smartWrap(self, ci, fullname):
        '''
        Wraps fullname with Ptr<> if needed
        '''
        if self.isSmartClass(ci):
            return "Ptr<" + fullname + ">"
        return fullname


def copy_objc_files(objc_files_dir, objc_base_path, module_path):
    global total_files, updated_files
    objc_files = []
    re_filter = re.compile(r'^.+\.(h|m|mm|swift)(.in)?$')
    for root, dirnames, filenames in os.walk(objc_files_dir):
       objc_files += [os.path.join(root, filename) for filename in filenames if re_filter.match(filename)]
    objc_files = [f.replace('\\', '/') for f in objc_files]

    re_prefix = re.compile(r'^.+[\+/]([^\+]+)\.(h|m|mm|swift)(.in)?$')
    for objc_file in objc_files:
        src = checkFileRemap(objc_file)
        m = re_prefix.match(objc_file)
        target_fname = (m.group(1) + '.' + m.group(2)) if m else os.path.basename(objc_file)
        dest = os.path.join(objc_base_path, os.path.join(module_path, target_fname))
        assert dest[-3:] != '.in', dest + ' | ' + target_fname
        mkdir_p(os.path.dirname(dest))
        total_files += 1
        if (not os.path.exists(dest)) or (os.stat(src).st_mtime - os.stat(dest).st_mtime > 1):
            copyfile(src, dest)
            updated_files += 1

def sanitize_documentation_string(doc, type):
    lines = doc.splitlines()

    lines = list(map(lambda x: x[x.find('*'):].strip() if x.lstrip().startswith("*") else x, lines))
    lines = list(map(lambda x: "* " + x[1:].strip() if x.startswith("*") and x != "*" else x, lines))
    lines = list(map(lambda x: x if x.startswith("*") else "* " + x if x and x != "*" else "*", lines))

    hasValues = False
    for line in lines:
        if line != "*":
            hasValues = True
            break
    return "/**\n " + "\n ".join(lines) + "\n */" if hasValues else ""

if __name__ == "__main__":
    # initialize logger
    logging.basicConfig(filename='gen_objc.log', format=None, filemode='w', level=logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.WARNING)
    logging.getLogger().addHandler(handler)

    # parse command line parameters
    import argparse
    arg_parser = argparse.ArgumentParser(description='OpenCV Objective-C Wrapper Generator')
    arg_parser.add_argument('-p', '--parser', required=True, help='OpenCV header parser')
    arg_parser.add_argument('-c', '--config', required=True, help='OpenCV modules config')

    args=arg_parser.parse_args()

    # import header parser
    hdr_parser_path = os.path.abspath(args.parser)
    if hdr_parser_path.endswith(".py"):
        hdr_parser_path = os.path.dirname(hdr_parser_path)
    sys.path.append(hdr_parser_path)
    import hdr_parser

    with open(args.config) as f:
        config = json.load(f)

    ROOT_DIR = config['rootdir']; assert os.path.exists(ROOT_DIR)
    FILES_REMAP = { os.path.realpath(os.path.join(ROOT_DIR, f['src'])): f['target'] for f in config['files_remap'] }
    logging.info("\nRemapped configured files (%d):\n%s", len(FILES_REMAP), pformat(FILES_REMAP))

    dstdir = "./gen"
    objc_base_path = os.path.join(dstdir, 'objc'); mkdir_p(objc_base_path)
    objc_test_base_path = os.path.join(dstdir, 'test'); mkdir_p(objc_test_base_path)

    for (subdir, target_subdir) in [('src/objc', 'objc')]:
        if target_subdir is None:
            target_subdir = subdir
        objc_files_dir = os.path.join(SCRIPT_DIR, subdir)
        if os.path.exists(objc_files_dir):
            target_path = os.path.join(dstdir, target_subdir); mkdir_p(target_path)
            copy_objc_files(objc_files_dir, target_path)

    # launch Objective-C Wrapper generator
    generator = ObjectiveCWrapperGenerator()

    gen_dict_files = []

    print("Objective-C: Processing OpenCV modules: %d" % len(config['modules']))
    for e in config['modules']:
        (module, module_location) = (e['name'], os.path.join(ROOT_DIR, e['location']))
        logging.info("\n=== MODULE: %s (%s) ===\n" % (module, module_location))

        module_imports = []
        module_header_code = ""
        module_body_code = ""
        srcfiles = []
        common_headers = []

        misc_location = os.path.join(module_location, 'misc/objc')

        srcfiles_fname = os.path.join(misc_location, 'filelist')
        if os.path.exists(srcfiles_fname):
            with open(srcfiles_fname) as f:
                srcfiles = [os.path.join(module_location, str(l).strip()) for l in f.readlines() if str(l).strip()]
        else:
            re_bad = re.compile(r'(private|.inl.hpp$|_inl.hpp$|.details.hpp$|_winrt.hpp$|/cuda/|/legacy/)')
            # .h files before .hpp
            h_files = []
            hpp_files = []
            for root, dirnames, filenames in os.walk(os.path.join(module_location, 'include')):
               h_files += [os.path.join(root, filename) for filename in fnmatch.filter(filenames, '*.h')]
               hpp_files += [os.path.join(root, filename) for filename in fnmatch.filter(filenames, '*.hpp')]
            srcfiles = h_files + hpp_files
            srcfiles = [f for f in srcfiles if not re_bad.search(f.replace('\\', '/'))]
        logging.info("\nFiles (%d):\n%s", len(srcfiles), pformat(srcfiles))

        common_headers_fname = os.path.join(misc_location, 'filelist_common')
        if os.path.exists(common_headers_fname):
            with open(common_headers_fname) as f:
                common_headers = [os.path.join(module_location, str(l).strip()) for l in f.readlines() if str(l).strip()]
        logging.info("\nCommon headers (%d):\n%s", len(common_headers), pformat(common_headers))

        gendict_fname = os.path.join(misc_location, 'gen_dict.json')
        if os.path.exists(gendict_fname):
            with open(gendict_fname) as f:
                gen_type_dict = json.load(f)
            class_ignore_list += gen_type_dict.get("class_ignore_list", [])
            const_ignore_list += gen_type_dict.get("const_ignore_list", [])
            const_private_list += gen_type_dict.get("const_private_list", [])
            missing_consts.update(gen_type_dict.get("missing_consts", {}))
            type_dict.update(gen_type_dict.get("type_dict", {}))
            ManualFuncs.update(gen_type_dict.get("ManualFuncs", {}))
            func_arg_fix.update(gen_type_dict.get("func_arg_fix", {}))
            namespaces_dict.update(gen_type_dict.get("namespaces_dict", {}))
            if 'module_objc_h_code' in gen_type_dict:
                module_header_code = read_contents(checkFileRemap(os.path.join(misc_location, gen_type_dict['module_objc_h_code'])))
            if 'module_objc_mm_code' in gen_type_dict:
                module_body_code = read_contents(checkFileRemap(os.path.join(misc_location, gen_type_dict['module_objc_mm_code'])))
            module_imports += gen_type_dict.get("module_imports", [])

        objc_files_dir = os.path.join(misc_location, 'src')
        if os.path.exists(objc_files_dir):
            copy_objc_files(objc_files_dir, objc_base_path, module)

        objc_test_files_dir = os.path.join(misc_location, 'test')
        if os.path.exists(objc_test_files_dir):
            copy_objc_files(objc_test_files_dir, objc_test_base_path, 'test' + module)

        if len(srcfiles) > 0:
            generator.gen(srcfiles, module, dstdir, objc_base_path, common_headers)
        else:
            logging.info("No generated code for module: %s", module)

    print('Generated files: %d (updated %d)' % (total_files, updated_files))

/* Read existing COMSOL state in a private standalone JVM. Never build or solve. */
import java.io.*;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

public final class ReadMph {
  static final List<Object> nodes = new ArrayList<>();
  static final List<String> warnings = new ArrayList<>();
  static final int MAX_NODES = 5000, MAX_PROPERTIES = 500, MAX_ARRAY = 5000;
  static int propertyErrors = 0, selectionErrors = 0, enumerationErrors = 0;
  static int propertyTotal = 0, propertyObserved = 0, propertyPartial = 0, nonFinite = 0;
  static int selectionTotal = 0, selectionFields = 0, selectionObserved = 0, unsupportedSelectionGetters = 0;
  static int unsupportedSelectionAccessors = 0, propertyDeclared = 0;
  static final Map<String,Integer> failureReasons = new TreeMap<>();
  static final Map<String,Integer> truncationReasons = new TreeMap<>();
  static final IdentityHashMap<Object,Integer> propertyCounts = new IdentityHashMap<>();
  static boolean truncated = false;
  static final Set<String> visited = new HashSet<>();

  static Method method(Object target, String name, Object... args) throws NoSuchMethodException {
    Class<?> type = target instanceof Class ? (Class<?>) target : target.getClass();
    for (Method method : type.getMethods()) {
      if (!method.getName().equals(name) || method.getParameterCount() != args.length) continue;
      Class<?>[] parameters = method.getParameterTypes(); boolean match = true;
      for (int i=0;i<args.length;i++) {
        Class<?> p=parameters[i];
        if (p.isPrimitive()) p = p==boolean.class ? Boolean.class : p==int.class ? Integer.class : p==double.class ? Double.class : p;
        if (args[i]!=null && !p.isInstance(args[i])) {match=false;break;}
      }
      if (!match) continue;
      return method;
    }
    throw new NoSuchMethodException(name);
  }
  static Object call(Object target, String name, Object... args) throws Exception {
    try {return method(target,name,args).invoke(target instanceof Class ? null : target,args);}
    catch(InvocationTargetException error) {
      Throwable cause=error.getCause();
      if(cause instanceof Exception) throw (Exception)cause;
      throw new RuntimeException(cause==null ? error : cause);
    }
  }
  static boolean supports(Object target,String name,Object... args) {
    try {method(target,name,args);return true;}catch(NoSuchMethodException error){return false;}
  }
  static Object optional(Object target,String name,Object... args) {
    try {return call(target,name,args);} catch(Exception e) {return null;}
  }
  static String text(Object value) {return value==null ? "" : String.valueOf(value);}
  static Map<String,Object> map(Object... values) {
    Map<String,Object> out=new LinkedHashMap<>();
    for(int i=0;i<values.length;i+=2) out.put((String)values[i],values[i+1]);return out;
  }
  static void limit(String code) {truncated=true;truncationReasons.put(code,truncationReasons.getOrDefault(code,0)+1);}
  static Object bounded(Object value,int depth) {
    if(value instanceof Double && !Double.isFinite((Double)value) || value instanceof Float && !Float.isFinite((Float)value)) {
      nonFinite++;return map("numeric_status","non_finite","representation",String.valueOf(value),"encoding","tagged_api_number");
    }
    if(value==null || value instanceof Number || value instanceof Boolean) return value;
    if(depth>5) {limit("value_depth");return "[nested value omitted]";}
    if(value.getClass().isArray()) {
      List<Object> out=new ArrayList<>();int size=Array.getLength(value);
      for(int i=0;i<Math.min(size,MAX_ARRAY);i++) out.add(bounded(Array.get(value,i),depth+1));
      if(size>MAX_ARRAY){limit("array_items");out.add("[array truncated; total="+size+"]");}return out;
    }
    String result=String.valueOf(value);
    if(result.length()>16000){limit("text_characters");return result.substring(0,16000)+" [truncated]";}return result;
  }
  static Map<String,Object> error(Exception error,String getter,String reason) {
    String message=text(error.getMessage());
    if(message.length()>1000)message=message.substring(0,1000)+" [truncated]";
    String origin=reason.equals("no_value_type") || reason.equals("unsupported_value_type") ? "extractor_type_dispatch" : error instanceof NoSuchMethodException ? "reflection" : "api_invocation";
    return map("reason",reason,"origin",origin,"getter",getter,"exception",error.getClass().getName(),"message",message);
  }
  static void reason(String code) {failureReasons.put(code,failureReasons.getOrDefault(code,0)+1);}
  static void unresolved(Map<String,Object> item,Exception exception,String getter,String code) {
    propertyErrors++;reason(code);item.put("read_status","unresolved");
    item.put("error_type",exception.getClass().getSimpleName());item.put("read_error",error(exception,getter,code));
  }
  static List<Object> properties(Object node) {
    List<Object> out=new ArrayList<>();Object names;
    if(!supports(node,"properties"))return out;
    try{names=call(node,"properties");}catch(Exception exception){enumerationErrors++;reason("property_enumeration_failed");return out;}
    if(names==null || !names.getClass().isArray()) return out;
    int size=Array.getLength(names);propertyCounts.put(node,size);propertyDeclared+=size;if(size>MAX_PROPERTIES)limit("properties_per_node");
    for(int i=0;i<Math.min(size,MAX_PROPERTIES);i++) {
      propertyTotal++;String key=text(Array.get(names,i));String type;
      Map<String,Object> item=map("name",key,"value_origin","unknown_default_or_explicit","effective_status","not_evaluated");
      try{type=text(call(node,"getValueType",key));item.put("value_type",type);}
      catch(Exception exception){item.put("value_type",null);unresolved(item,exception,"getValueType","value_type_unavailable");out.add(item);continue;}
      String getter="";
      switch(type.toLowerCase(Locale.ROOT)) {
        case "file":case "string":getter="getString";break;case "stringarray":getter="getStringArray";break;
        case "stringmatrix":getter="getStringMatrix";break;case "boolean":getter="getBoolean";break;
        case "booleanarray":getter="getBooleanArray";break;case "int":getter="getInt";break;
        case "intarray":getter="getIntArray";break;case "intmatrix":getter="getIntMatrix";break;
        case "double":getter="getDouble";break;case "doublearray":getter="getDoubleArray";break;
        case "doublerowmatrix":case "doublematrix":getter="getDoubleMatrix";break;
      }
      try {
        if(type.equalsIgnoreCase("Selection")) {
          item.put("getter","selection(String)");
          Map<String,Object> value=selectionValue(call(node,"selection",key));
          item.put("value",value);
          if(value!=null && Boolean.TRUE.equals(value.get("read_complete"))) {item.put("read_status","observed");propertyObserved++;}
          else {item.put("read_status","partial");propertyPartial++;reason("selection_property_incomplete");}
        } else if(getter.isEmpty()) {
          String code=type.isEmpty() || type.equalsIgnoreCase("None") ? "no_value_type" : "unsupported_value_type";
          unresolved(item,new UnsupportedOperationException("COMSOL returned value type "+type+"; no value getter is selected. No action or geometry/solver execution is attempted."),"",code);
        } else {
          item.put("getter",getter);item.put("value",bounded(call(node,getter,key),0));item.put("read_status","observed");propertyObserved++;
        }
      } catch(Exception exception) {unresolved(item,exception,getter.isEmpty()?"selection(String)":getter,exception instanceof NoSuchMethodException ? "getter_unavailable" : "api_getter_failed");}
      out.add(item);
    }return out;
  }
  static Map<String,Object> selectionValue(Object selection) {
    if(selection==null){selectionErrors++;reason("selection_unavailable");return map("read_complete",false,"status","unresolved","reason","selection accessor returned null");}
    selectionTotal++;int before=selectionErrors;boolean observed=false;
    Map<String,Object> out=map("effective_membership","not_validated","geometry_built_by_extractor",false,
      "scope","API getter observations only; entity indices are not cross-edit persistent identities");
    List<String> unsupported=new ArrayList<>();Object objects=null;
    for(String getter:new String[]{"named","dim","dimension","geom","isGlobal","isGeom","isInheriting","inputEntities","entities","objects"}) {
      if(!supports(selection,getter)){unsupported.add(getter);unsupportedSelectionGetters++;continue;}
      selectionFields++;
      try {
        Object value=call(selection,getter);observed=true;selectionObserved++;
        out.put(getter,map("value",bounded(value,0),"status","observed","getter",getter+"()"));
        if(getter.equals("objects"))objects=value;
      } catch(Exception exception){selectionErrors++;reason("selection_getter_failed");out.put(getter,map("status","unresolved","error_type",exception.getClass().getSimpleName(),"read_error",error(exception,getter+"()","selection_getter_failed")));}
    }
    // Geometry-object selections use entities(objectName), unlike entity selections.
    if(objects!=null && objects.getClass().isArray()) {
      List<Object> perObject=new ArrayList<>();int count=Array.getLength(objects);
      if(count>200)limit("selection_objects");
      for(int i=0;i<Math.min(200,count);i++) {
        String name=text(Array.get(objects,i));if(!supports(selection,"entities",name))continue;
        selectionFields++;
        try{perObject.add(map("object",name,"entities",bounded(call(selection,"entities",name),0),"status","observed","getter","entities(String)"));selectionObserved++;}
        catch(Exception exception){selectionErrors++;reason("selection_getter_failed");perObject.add(map("object",name,"status","unresolved","read_error",error(exception,"entities(String)","selection_getter_failed")));}
      }
      out.put("object_entities",perObject);
    }
    if(!observed){selectionErrors++;reason("selection_getters_unavailable");}
    out.put("unsupported_getters",unsupported);out.put("read_complete",observed && before==selectionErrors);
    return out;
  }
  static Map<String,Object> selection(Object node) {
    if(!supports(node,"selection"))return null;
    if(supports(node,"hasSelection") && Boolean.FALSE.equals(optional(node,"hasSelection")))return map("status","not_applicable","reason","hasSelection() returned false");
    try{
      Object value=call(node,"selection");
      if(value!=null && supports(value,"tags") && !supports(value,"named") && !supports(value,"entities") && !supports(value,"objects"))return map("status","not_applicable","reason","selection() returns a feature collection, enumerated separately");
      return selectionValue(value);
    } catch(Exception exception){
      String message=text(exception.getMessage());
      if(exception instanceof UnsupportedOperationException || message.equals("Method not supported") || message.endsWith(": Method not supported")) {
        unsupportedSelectionAccessors++;
        return map("status","not_applicable","reason","API explicitly reports this default selection accessor is unsupported",
          "read_error",error(exception,"selection()","default_selection_not_supported"));
      }
      selectionErrors++;reason("selection_accessor_failed");return map("read_complete",false,"status","unresolved","read_error",error(exception,"selection()","selection_accessor_failed"));
    }
  }
  static void walk(Object node,String path,String category,int depth) {
    if(depth>20 || nodes.size()>=MAX_NODES){limit(depth>20?"node_depth":"node_count");return;}
    if(!visited.add(path))return;
    Map<String,Object> record=map("path",path,"category",category,"tag",text(optional(node,"tag")),
      "label",text(optional(node,"label")),"type",text(optional(node,"getType")),
      "local_active",optional(node,"isActive"),"effective_activation","not_evaluated",
      "properties",properties(node),"selection",selection(node));nodes.add(record);
    if(propertyCounts.containsKey(node)){record.put("properties_total",propertyCounts.get(node));record.put("properties_truncated",propertyCounts.get(node)>MAX_PROPERTIES);}
    if(category.equals("sol")) {
      for(String getter:new String[]{"study","isEmpty","isInitialized","isAutomatic"}) {
        try{record.put(getter,bounded(call(node,getter),0));}catch(Exception e){}
      }
    }
    for(String branch:new String[]{"feature","propertyGroup","prop"}) branch(node,path,branch,depth+1);
    if(category.equals("component")) for(String branch:new String[]{"geom","mesh","physics","material","multiphysics","selection","variable","func","cpl","coordSystem","pair","probe"}) branch(node,path,branch,depth+1);
  }
  static void branch(Object parent,String path,String branch,int depth) {
    if(!supports(parent,branch))return;
    Object list,tags;
    try{list=call(parent,branch);if(list==null)return;tags=call(list,"tags");}
    catch(Exception exception){enumerationErrors++;reason("branch_enumeration_failed");if(warnings.size()<200)warnings.add("Could not enumerate "+path+"/"+branch+": "+exception.getClass().getSimpleName());return;}
    if(tags==null || !tags.getClass().isArray())return;
    for(int i=0;i<Array.getLength(tags);i++) {
      String tag=text(Array.get(tags,i));
      try {walk(call(parent,branch,tag),path+"/"+branch+"("+tag+")",branch,depth);}
      catch(Exception e){enumerationErrors++;reason("node_enumeration_failed");if(warnings.size()<200)warnings.add("Could not enumerate "+path+"/"+branch+"("+tag+")");}
      if(nodes.size()>=MAX_NODES){limit("node_count");break;}
    }
  }
  static String quote(String value) {
    StringBuilder out=new StringBuilder("\"");for(char c:value.toCharArray()){
      switch(c){case '"':out.append("\\\"");break;case '\\':out.append("\\\\");break;
        case '\n':out.append("\\n");break;case '\r':out.append("\\r");break;case '\t':out.append("\\t");break;
        default:if(c<32)out.append(String.format("\\u%04x",(int)c));else out.append(c);}
    }return out.append('"').toString();
  }
  static String json(Object value) {
    if(value instanceof Double && !Double.isFinite((Double)value) || value instanceof Float && !Float.isFinite((Float)value))return json(bounded(value,0));
    if(value==null)return "null";if(value instanceof Boolean || value instanceof Number)return String.valueOf(value);
    if(value instanceof Map){StringJoiner j=new StringJoiner(",","{","}");for(Object entry:((Map<?,?>)value).entrySet()){
      Map.Entry<?,?> e=(Map.Entry<?,?>)entry;j.add(quote(String.valueOf(e.getKey()))+":"+json(e.getValue()));}return j.toString();}
    if(value instanceof Iterable){StringJoiner j=new StringJoiner(",","[","]");for(Object x:(Iterable<?>)value)j.add(json(x));return j.toString();}
    return quote(String.valueOf(value));
  }
  public static void main(String[] args) {
    Object model=null;Class<?> util=null;
    try {
      util=Class.forName("com.comsol.model.util.ModelUtil");call(util,"initStandalone",false);optional(util,"showProgress",false);
      model=call(util,"loadCopy","LibraryExtract",args[0]);
      for(String name:new String[]{"component","study","sol","material","selection","variable","func","cpl"}) branch(model,"model",name,0);
      Object result=optional(model,"result");if(result!=null)walk(result,"model/result","result",0);
      String exportStatus="not_requested";
      if(args.length>2)try{call(model,"save",args[2],"java");exportStatus="exported_reconstruction_not_executed";}catch(Exception e){exportStatus="failed";warnings.add("Java export failed: "+e.getClass().getSimpleName());}
      Map<String,Object> payload=map("schema","mph-api-state/v1","runtime_version",text(optional(util,"getComsolVersion")),
        "file_saved_version",text(optional(model,"getComsolVersion")),"nodes",nodes,"warnings",warnings,
        "read_errors",propertyErrors+selectionErrors+enumerationErrors,"truncated",truncated,
        "read_contract","mph-api-read/v2",
        "read_coverage",map("properties",map("total",propertyTotal,"declared",propertyDeclared,"observed",propertyObserved,"partial",propertyPartial,"unresolved",propertyErrors),
          "selections",map("total",selectionTotal,"fields_attempted",selectionFields,"fields_observed",selectionObserved,"unresolved",selectionErrors,"unsupported_getters",unsupportedSelectionGetters,"unsupported_accessors",unsupportedSelectionAccessors),
          "traversal",map("unresolved",enumerationErrors),"non_finite_values",nonFinite,"failure_reasons",failureReasons),
        "limits",map("max_nodes",MAX_NODES,"max_properties_per_node",MAX_PROPERTIES,"max_array_items",MAX_ARRAY,"max_selection_objects",200),"truncation_reasons",truncationReasons,
        "export_status",exportStatus,"geometry_run",false,"mesh_run",false,"solver_run",false,"defaults_generated",false,"physical_validation","not_run");
      Files.write(Paths.get(args[1]),json(payload).getBytes(StandardCharsets.UTF_8));
      optional(util,"remove","LibraryExtract");System.out.println("MPH_API_EXTRACTION_OK nodes="+nodes.size());System.exit(0);
    }catch(Throwable error){System.err.println("MPH_API_EXTRACTION_FAILED "+error.getClass().getSimpleName());error.printStackTrace();System.exit(2);}
  }
}
